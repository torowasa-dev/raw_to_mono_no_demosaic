#!/usr/bin/env python3
"""Bayer RAWを補間なしでモノクロ化。自動明度の16bit TIFF／線形モノクロDNG。

2×2のRGB Bayerブロックを平均し、縦横1/2で保存する。
TIFF: 自動補正で平均明度50%、明部を滑らかに圧縮。既定ガンマ2.0。
DNG: 線形画素を保存し、自動露出はBaselineExposureに記録する。
保存は可逆圧縮（既定）または無圧縮。元RAWは変更しない。
"""
from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import sys
import tempfile

import numpy as np
import rawpy
import tifffile


def finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError("有限の数値を指定してください")
    return number


def read_mono(input_path: Path, mode: str, wb: str) -> np.ndarray:
    """黒補正／正規化／任意のWB後、重ならない2×2を平均する。"""
    with rawpy.imread(str(input_path)) as raw:
        sensor = raw.raw_image_visible
        if sensor.ndim != 2:
            raise ValueError("2次元のRAWのみ対応しています (RGB/Foveon等は対象外)")
        pattern = raw.raw_pattern
        desc = raw.color_desc.decode("ascii", errors="replace")
        if (pattern is None or pattern.shape != (2, 2)
                or sorted(desc[int(i)] for i in pattern.flat) != ["B", "G", "G", "R"]):
            raise ValueError("2×2 RGB Bayer配列のみ対応。X-Trans等は対象外です")
        height = sensor.shape[0] // 2 * 2
        width = sensor.shape[1] // 2 * 2
        if height == 0 or width == 0:
            raise ValueError("RAW画像が小さすぎます")
        mono = np.empty((height // 2, width // 2), dtype=np.uint16 if mode == "codes" else np.float32)
        if mode == "codes":
            for y in range(0, height, 256):
                block = sensor[y:min(y + 256, height), :width].astype(np.float32)
                mean = block.reshape(block.shape[0] // 2, 2, width // 2, 2).mean(axis=(1, 3))
                mono[y // 2:y // 2 + mean.shape[0]] = np.rint(mean).astype(np.uint16)
        else:
            colors = raw.raw_colors_visible
            black = np.asarray(raw.black_level_per_channel, dtype=np.float32)
            white = np.full(black.shape, float(raw.white_level), dtype=np.float32)
            per_channel_white = raw.camera_white_level_per_channel
            if per_channel_white is not None:
                for i, value in enumerate(per_channel_white[:len(white)]):
                    if value is not None and value > black[i]:
                        white[i] = value
            used = np.unique(colors)
            if colors.shape != sensor.shape or used.max() >= len(black):
                raise ValueError("CFA/黒レベル情報が不整合です")
            if np.any(white[used] <= black[used]):
                raise ValueError("飽和レベルが黒レベル以下です")
            gain = np.ones(black.shape, dtype=np.float32)
            if wb == "camera":
                gain = np.asarray(raw.camera_whitebalance, dtype=np.float32).copy()
                # 一部RAWは2番目の緑のWBを0で示す。
                for i in used:
                    if gain[i] <= 0 and i < len(desc) and desc[i] == "G":
                        for j, label in enumerate(desc[:len(gain)]):
                            if label == "G" and gain[j] > 0:
                                gain[i] = gain[j]
                                break
                if np.any(~np.isfinite(gain[used])) or np.any(gain[used] <= 0):
                    raise ValueError("有効なカメラWBがありません。--wb noneを使用してください")
                gain /= gain[used].max()  # WB単独では新たに白飛びさせない。
            scale = 1.0 / np.maximum(white - black, 1)
            for y in range(0, height, 256):
                end = min(y + 256, height)
                c = colors[y:end, :width]
                tone = sensor[y:end, :width].astype(np.float32)
                tone -= black[c]
                tone *= scale[c]
                # センサーの黒/白範囲にクリップし、線形光量のまま平均する。
                np.clip(tone, 0.0, 1.0, out=tone)
                tone *= gain[c]
                tone = tone.reshape(tone.shape[0] // 2, 2, width // 2, 2).mean(axis=(1, 3))
                mono[y // 2:y // 2 + tone.shape[0]] = tone

    return mono


def render_tone(values: np.ndarray, ev: float, gamma: float, *, soft: bool) -> np.ndarray:
    gain = 2.0 ** ev
    if soft:
        # 単調な肩: 0→0、1→1。ゲインによる新たな飽和を避ける。
        tone = values / (values + (1.0 - values) / gain)
    else:
        tone = np.clip(values * gain, 0.0, 1.0)
    return np.power(tone, 1.0 / gamma)


def auto_ev(values: np.ndarray, target: float, gamma: float, *, soft: bool) -> float:
    # 各ビンの実測平均で高速に探索。黒0と白1は別のビンで厳密に保持。
    indices = np.floor(np.sqrt(values.ravel()) * 65536).astype(np.int32)
    indices[(values.ravel() > 0) & (indices == 0)] = 1
    counts = np.bincount(indices, minlength=65537)
    sums = np.bincount(indices, weights=values.ravel(), minlength=65537)
    used = counts > 0
    centers = sums[used] / counts[used]
    weights = counts[used] / values.size
    if np.all((centers == 0) | (centers == 1)):
        return 0.0
    low, high = -16.0, 16.0
    for _ in range(40):
        mid = (low + high) / 2.0
        mean = np.dot(render_tone(centers, mid, gamma, soft=soft), weights)
        if mean < target:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def write_image(path: Path, pixels: np.ndarray, *, dng: bool, ev: float,
                description: str, compression: str, force: bool) -> None:
    tags = []
    if dng:
        h, w = pixels.shape
        tags = [
            (274, 'H', 1, 1, False),  # Sensor orientation: do not rotate data.
            (50706, 'B', 4, (1, 4, 0, 0), False),
            (50707, 'B', 4, (1, 1, 0, 0), False),
            (50708, 's', 0, 'BayerMono 2x2 mean', False),
            (50714, 'H', 1, 0, False), (50717, 'I', 1, 65535, False),
            (50718, '2I', 2, (1, 1, 1, 1), False),
            (50719, 'I', 2, (0, 0), False), (50720, 'I', 2, (w, h), False),
            (50730, '2i', 1, (round(ev * 1000000), 1000000), False),
            (50731, '2I', 1, (1, 1), False), (50732, '2I', 1, (1, 1), False),
            (50738, '2I', 1, (1, 1), False),
        ]
    codec = None if compression == 'none' else ('jpeg' if dng else 'deflate')
    # DNG JPEG here is 16bit lossless SOF3, never ordinary lossy JPEG.
    codec_args = {'lossless': True, 'bitspersample': 16} if codec == 'jpeg' else None
    fd, temporary = tempfile.mkstemp(prefix=path.stem + '.', suffix='.tmp', dir=path.parent)
    os.close(fd)
    try:
        tifffile.imwrite(temporary, pixels, photometric=34892 if dng else 'minisblack',
                         compression=codec, compressionargs=codec_args,
                         tile=(256, 256) if dng else None,
                         rowsperstrip=None if dng else 256, metadata=None, description=description,
                         extratags=tags)
        # Compression must preserve every stored integer pixel, including DNG.
        if not np.array_equal(tifffile.imread(temporary), pixels):
            raise RuntimeError('保存後の画素値が一致しません')
        if force:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)  # Atomic no-overwrite publication.
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def convert(input_path: Path, output_path: Path, *, mode: str = 'linear',
            wb: str = 'none', gamma: float | None = None, exposure: float = 0.0,
            auto_exposure: bool | None = None, target_level: float = 0.5,
            compression: str = 'lossless', force: bool = False,
            report: dict | None = None) -> tuple[int, int]:
    input_path, output_path = Path(input_path), Path(output_path)
    if input_path.resolve() == output_path.resolve():
        raise ValueError('入力と出力を同じファイルにできません')
    if output_path.suffix.lower() not in {'.tif', '.tiff', '.dng'}:
        raise ValueError('出力拡張子は .tif/.tiff/.dng にしてください')
    if output_path.exists() and not force:
        raise FileExistsError(f'出力が存在します: {output_path} (--forceで上書き)')
    if mode not in {'linear', 'codes'} or wb not in {'none', 'camera'}:
        raise ValueError('不正なモードまたはWB指定')
    dng = output_path.suffix.lower() == '.dng'
    auto_exposure = mode == 'linear' if auto_exposure is None else auto_exposure
    gamma = (1.0 if mode == 'codes' or dng else 2.0) if gamma is None else gamma
    if not math.isfinite(gamma) or gamma <= 0:
        raise ValueError('gammaは正の有限数にしてください')
    if not math.isfinite(exposure) or not -32 <= exposure <= 32:
        raise ValueError('exposureは-32〜32 EVにしてください')
    if not math.isfinite(target_level) or not 0 < target_level < 1:
        raise ValueError('target-levelは0より大きく1未満にしてください')
    if compression not in {'lossless', 'none'}:
        raise ValueError('保存は可逆圧縮または無圧縮のみ対応')
    if mode == 'codes' and (dng or wb != 'none' or gamma != 1 or exposure != 0 or auto_exposure):
        raise ValueError('codesモードは補正なしTIFF専用です')
    if dng and gamma != 1:
        raise ValueError('DNGは線形画素を保持するため --gamma は1のみ対応')
    mono = read_mono(input_path, mode, wb)
    ev = exposure
    mean = None
    if mode == 'linear':
        # DNGの現像指示は通常の線形EV。表示明度の推定にはガンマ2を用いる。
        view_gamma = 2.0 if dng else gamma
        if auto_exposure:
            ev += auto_ev(mono, target_level, view_gamma, soft=not dng)
        rendered = render_tone(mono, ev, view_gamma, soft=auto_exposure and not dng)
        mean = float(rendered.mean(dtype=np.float64))
        if auto_exposure and exposure == 0 and abs(mean - target_level) > 0.005:
            print(f'注意: 黒/白の比率または補正範囲により目標明度に届きません ({mean:.4f})',
                  file=sys.stderr)
        pixels = np.rint((mono if dng else rendered) * 65535).astype(np.uint16)
    else:
        pixels = mono
    description = (f'No demosaic; 2x2 Bayer mean; mode={mode}; wb={wb}; '
                   f'auto_exposure={auto_exposure}; target={target_level}; '
                   f'exposure_ev={ev:.6f}; gamma={gamma}; '
                   'sensor orientation; CFA response retained')
    write_image(output_path, pixels, dng=dng, ev=ev, description=description,
                compression=compression, force=force)
    if report is not None:
        report.update(exposure_ev=ev, display_mean=mean, dng=dng)
    return pixels.shape


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('input', type=Path, help='入力Bayer RAW')
    parser.add_argument('-o', '--output', type=Path, help='出力TIFFまたはDNG')
    parser.add_argument('--format', choices=('tiff', 'dng', 'both'), default=None,
                        help='既定tiff。bothはTIFFとDNGを生成')
    parser.add_argument('--mode', choices=('linear', 'codes'), default='linear')
    parser.add_argument('--wb', choices=('none', 'camera'), default='none')
    parser.add_argument('--gamma', type=finite_float, default=None,
                        help='TIFF表示ガンマ (既定2.0)、DNG/codesは1のみ')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--auto-exposure', dest='auto_exposure', action='store_true')
    group.add_argument('--no-auto-exposure', dest='auto_exposure', action='store_false')
    parser.set_defaults(auto_exposure=None)
    parser.add_argument('--target-level', type=finite_float, default=0.5, help='目標平均明度0〜1')
    parser.add_argument('--exposure', type=finite_float, default=0.0, help='自動補正に追加するEV')
    parser.add_argument('--compression', choices=('lossless', 'none'), default='lossless')
    parser.add_argument('--force', action='store_true', help='出力の上書きを許可')
    args = parser.parse_args()
    if args.output and args.format:
        parser.error('-oと--formatは同時に指定できません')
    if args.format == 'both' and args.gamma not in {None, 1}:
        parser.error('bothでは--gammaを省略してください (TIFFは既定2、DNGは線形)')
    if args.mode == 'codes' and args.format in {'dng', 'both'}:
        parser.error('codesモードはTIFF専用です')
    extensions = ('.tif', '.dng') if args.format == 'both' else ('.dng',) if args.format == 'dng' else ('.tif',)
    outputs = [args.output] if args.output else [args.input.with_name(args.input.stem + '_mono_' + args.mode + ext) for ext in extensions]
    if not args.force:
        for output in outputs:
            if output.exists():
                print(f'変換失敗: 出力が存在します: {output} (--forceで上書き)', file=sys.stderr)
                return 1
    try:
        for output in outputs:
            report = {}
            height, width = convert(args.input, output, mode=args.mode, wb=args.wb,
                                    gamma=args.gamma, exposure=args.exposure,
                                    auto_exposure=args.auto_exposure, target_level=args.target_level,
                                    compression=args.compression, force=args.force, report=report)
            print(f'保存: {output} ({width}×{height}, 16bit, 1ch, {args.compression})')
            if report['display_mean'] is not None:
                label = 'DNG現像指示/ガンマ2推定値' if report['dng'] else 'TIFF実測'
                print(f"  {label}: EV={report['exposure_ev']:+.3f}, 平均明度={report['display_mean']:.4f}")
    except Exception as exc:
        print(f'変換失敗: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
