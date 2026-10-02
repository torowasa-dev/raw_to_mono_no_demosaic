#!/usr/bin/env python3
"""Bayer RAWの2×2画素(R,G,G,B)から1階調画素の16bit TIFFを生成。

Python 3.10以上。インストール:
    python -m pip install numpy rawpy tifffile
実行例:
    python raw_to_mono_no_demosaic.py photo.NEF
    python raw_to_mono_no_demosaic.py photo.ARW -o mono.tif --gamma 2.2
    python raw_to_mono_no_demosaic.py photo.CR2 --mode codes
    python raw_to_mono_no_demosaic.py photo.RAF --wb camera --exposure -1

通常は画素ごとの黒レベルを引き、飽和レベルで正規化してから、
重ならない2×2ブロックの平均値を0〜65535で保存する。
--mode codes は rawpy が展開した画素コードの2×2平均を丸めて保存する。
R,G,G,Bの平均=(R+G1+G2+B)/4。縦横1/2、画素数1/4になる。
黒補正やWB後の平均も、測光的な輝度Yではなくセンサー信号の平均。
--gamma 1 (既定) は線形。2.2は表示用の簡易ガンマで、sRGBではない。
--wb camera は平均前に色別倍率を掛ける。最大倍率を1に正規化。

注意:
・カラーCFAを通した測定値なので、真の輝度やモノクロセンサー相当ではない。
・CFA格子はブロック平均で目立ちにくくなるが、色への感度差は残る。
・デモザイクは行わない。縮小は2×2平均だけ。回転/ノイズ除去は行わない。
・奇数寸法の末尾1列/1行は切り捨てる。
・rawpyの可視領域を保存。現像後の寸法/向きと異なる場合がある。
・読み込み対応はrawpy/LibRaw次第。2×2のRGB Bayer配列のみ対象。
・X-Trans、Foveon、モノクロRAW、既にRGB化したDNG等は拒否する。
・黒レベル補正はrawpyの色別定数のみ。行/列ごとの補正は行わない。
・原RAWは変更しない。出力上書きには --force が必要。
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import sys

import numpy as np
import rawpy
import tifffile


def finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError("有限の数値を指定してください")
    return number


def convert(input_path: Path, output_path: Path, *, mode: str = "linear",
            wb: str = "none", gamma: float = 1.0, exposure: float = 0.0,
            force: bool = False) -> tuple[int, int]:
    """補間なし。各2×2 Bayerブロックを1つのモノクロ画素へ平均する。"""
    if input_path.resolve() == output_path.resolve():
        raise ValueError("入力と出力を同じファイルにできません")
    if output_path.suffix.lower() not in {".tif", ".tiff"}:
        raise ValueError("出力拡張子は .tif または .tiff にしてください")
    if output_path.exists() and not force:
        raise FileExistsError(f"出力が存在します: {output_path} (--forceで上書き)")
    if mode not in {"linear", "codes"} or wb not in {"none", "camera"}:
        raise ValueError("不正なモードまたはWB指定")
    if not math.isfinite(gamma) or gamma <= 0:
        raise ValueError("gammaは正の有限数にしてください")
    if not math.isfinite(exposure) or not -32 <= exposure <= 32:
        raise ValueError("exposureは-32〜32 EVにしてください")
    if mode == "codes" and (wb != "none" or gamma != 1 or exposure != 0):
        raise ValueError("codesモードではWB/ガンマ/露出を指定できません")

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
        mono = np.empty((height // 2, width // 2), dtype=np.uint16)
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
                tone *= 2.0 ** exposure
                np.clip(tone, 0.0, 1.0, out=tone)
                if gamma != 1:
                    np.power(tone, 1.0 / gamma, out=tone)
                mono[y // 2:y // 2 + tone.shape[0]] = np.rint(tone * 65535).astype(np.uint16)

    # 1チャンネル、黒=0。TIFFへの可逆圧縮は画素値を変更しない。
    with output_path.open("wb" if force else "xb") as handle:
        tifffile.imwrite(handle, mono, photometric="minisblack",
                         compression="deflate", metadata=None,
                         description=f"No demosaic; 2x2 Bayer mean; mode={mode}; wb={wb}; "
                                     f"gamma={gamma}; exposure_ev={exposure}; "
                                     "sensor orientation; CFA response retained")
    return mono.shape


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="入力RAW")
    parser.add_argument("-o", "--output", type=Path, help="出力TIFF")
    parser.add_argument("--mode", choices=("linear", "codes"), default="linear")
    parser.add_argument("--wb", choices=("none", "camera"), default="none")
    parser.add_argument("--gamma", type=finite_float, default=1.0)
    parser.add_argument("--exposure", type=finite_float, default=0.0, help="EV補正")
    parser.add_argument("--force", action="store_true", help="出力の上書きを許可")
    args = parser.parse_args()
    output = args.output or args.input.with_name(args.input.stem + "_mono_" + args.mode + ".tif")
    try:
        height, width = convert(args.input, output, mode=args.mode, wb=args.wb,
                                gamma=args.gamma, exposure=args.exposure, force=args.force)
    except Exception as exc:
        print(f"変換失敗: {exc}", file=sys.stderr)
        return 1
    print(f"保存: {output} ({width}×{height}, 16bit, 1ch)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
