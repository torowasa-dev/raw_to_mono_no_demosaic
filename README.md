# raw_to_mono_no_demosaic

Bayer RAWから、デモザイクせずに16bitモノクロTIFF／DNGを生成します。
隣接する**2×2画素（R・G・G・B）の平均**を1画素として保存するため、縦横は1/2、総画素数は1/4です。

TIFFは画像ごとに自動補正し、表示用の平均明度を中央の50%に合わせます。
DNGは編集用の線形画素を保持し、自動露出の指示値をメタデータに保存します。
保存は可逆圧縮または無圧縮です。通常の非可逆JPEG出力はありません。

| 保存形式 | 内容 | 既定の圧縮 |
| --- | --- | --- |
| TIFF | 16bit・1チャンネル。自動明度補正と表示ガンマを適用 | Deflate（可逆） |
| DNG | 16bit・1チャンネルのLinearRaw。露出はBaselineExposureに記録 | lossless Huffman JPEG / SOF3（可逆） |

DNG内部のlossless JPEGは一般的な非可逆JPEGとは異なり、保存した整数画素を完全に復元できます。
保存のたびに読み戻し、圧縮前後の画素が一致することを確認してから出力を確定します。
2×2平均、黒補正、16bit整数への丸めは処理そのものなので、元RAWの4画素を復元する意味での可逆変換ではありません。

## インストール

Python 3.12と、以下の依存関係を使用します。

```bash
python -m venv .venv
```

| 環境 | 仮想環境の有効化 |
| --- | --- |
| Windows / PowerShell | `.venv\Scripts\Activate.ps1` |
| Windows / コマンドプロンプト | `.venv\Scripts\activate.bat` |
| macOS / Linux | `source .venv/bin/activate` |

```bash
python -m pip install -r requirements.txt
```

DNGの可逆圧縮にはimagecodecsを使用します。依存関係は動作確認したバージョンに固定しています。

## Windowsのドラッグ＆ドロップ

`raw_to_mono_no_demosaic.py.bat`にRAWを1枚または複数枚ドロップすると、各RAWの隣に**TIFFとDNGの両方**を保存します。
Pythonスクリプト、BAT、requirements.txtは同じフォルダーに置いてください。
初回は専用の`.venv`を作成し、起動時に依存関係を確認します。
Python Launcherの3.12を優先し、なければ既定のPythonを使用します。
依存関係の取得にはインターネット接続が必要です。

既存出力は上書きしません。失敗しても後続のRAWを処理し、最後に失敗件数を表示します。
Windows実機でのBAT操作は未検証です。

## コマンド例

```bash
# 自動明度補正TIFF（既定）
python raw_to_mono_no_demosaic.py photo.CR3

# TIFFとDNGの両方
python raw_to_mono_no_demosaic.py photo.ARW --format both

# 編集用の可逆圧縮モノクロDNG
python raw_to_mono_no_demosaic.py photo.CR3 -o mono.dng

# 無圧縮DNG
python raw_to_mono_no_demosaic.py photo.CR3 -o mono.dng --compression none

# 目標平均明度を40%に変更
python raw_to_mono_no_demosaic.py photo.ARW --target-level 0.4

# 自動補正に追加で+0.5 EV相当
python raw_to_mono_no_demosaic.py photo.ARW --exposure 0.5

# 従来の補正なし線形TIFF
python raw_to_mono_no_demosaic.py photo.CR3 --no-auto-exposure --gamma 1

# 黒補正・正規化を省いたセンサーコード平均のTIFF
python raw_to_mono_no_demosaic.py photo.CR3 --mode codes
```

自動命名は`photo_mono_linear.tif`／`photo_mono_linear.dng`です。
`codes`は補正なしTIFF専用で、WB・露出・ガンマ・自動露出は併用できません。

## 自動明度補正

「中央」は、ヒストグラムの最大ピークではなく**全画素の平均表示明度**で定義します。
既定は0〜1の範囲で0.5。固定の+2 EVを全画像へ適用せず、画像ごとに補正値を探索します。
撮影意図を判定するものではないため、夜景やハイキーなどでは`--target-level`または`--no-auto-exposure`で調整してください。
全黒・全白や既に飽和した画素の比率によって目標に届かない場合は、実測値と注意を表示します。

黒補正・正規化と任意のWBを行った後、線形値のまま2×2を平均します。
TIFFの自動補正では、平均値`M`に以下の単調曲線を適用します。

```text
g = 2 ** 補正EV
T = M / (M + (1 - M) / g)
出力TIFF = round(T ** (1 / gamma) * 65535)
```

黒0と白1を維持し、中間調を移動して明部を滑らかに圧縮します。
単純な乗算・クリップによる新たな白飛びを抑えます。16bit化による丸めはあります。
ガンマは平均後に適用し、既定2.0です。簡易べき乗変換で、厳密なsRGB変換ではありません。
自動補正時の表示EVはこの曲線のゲインを表し、画像全域で同じ倍率を掛ける線形露出とは異なります。
`--no-auto-exposure`のTIFFは従来どおり`clip(M * 2**exposure, 0, 1)`とガンマを適用します。

### DNGの露出

DNGの保存画素は常に`round(M * 65535)`で、ガンマやトーン曲線を焼き込みません。
自動露出は、通常の線形ゲインとガンマ2.0による表示推定値の平均が目標に近づくEVを求め、
`BaselineExposure`に記録します。現像ソフトがこの指示値を解釈して明るさを設定します。
画素を保持するため、現像時に白く見える明部も露出を下げて再調整できます。

DNGの実際のヒストグラムや明るさは、現像ソフト、プロファイル、トーン曲線にも依存します。
TIFFとDNGの表示が完全一致する保証はありません。DNGにTIFF用の非線形曲線を焼き込んでLinearRawと宣言する処理は行いません。
LibRawでの読み込みを確認していますが、Lightroom／Camera Rawでの表示は未検証です。

## オプション

| オプション | 既定 | 内容 |
| --- | --- | --- |
| `input` | 必須 | 入力Bayer RAW |
| `-o`, `--output` | 自動命名 | `.tif`／`.tiff`／`.dng`。`--format`とは排他 |
| `--format` | `tiff` | `tiff`／`dng`／`both`。BATは`both` |
| `--mode` | `linear` | `linear`／`codes` |
| `--wb` | `none` | `none`／`camera`。カメラWBは最大倍率を1に正規化 |
| `--auto-exposure` | linearで有効 | 自動明度補正を有効化 |
| `--no-auto-exposure` | 無効 | 自動補正を停止 |
| `--target-level` | `0.5` | 0より大きく1未満の目標平均表示明度 |
| `--exposure` | `0` | −32〜32 EV。自動補正値に追加 |
| `--gamma` | TIFF `2.0`、DNG/codes `1` | 正の有限数。DNG/codesは1のみ。bothでは省略推奨 |
| `--compression` | `lossless` | `lossless`（可逆）／`none`（無圧縮） |
| `--force` | 無効 | 既存出力の上書き |

## 仕組みの図解

![Bayer RAWの2×2平均](docs/illustrations/bayer_to_mono.png)

[拡大できるSVG版](docs/illustrations/bayer_to_mono.svg)

色はカラーフィルターを表し、各画素が持つのはフィルターを通ったセンサー信号です。
`M = (R + G1 + G2 + B) / 4`は維持しています。緑の重みは1/2、赤と青はそれぞれ1/4です。
欠けたRGB成分を周囲から補間する工程はありません。
図はTIFFの基本処理です。現在は平均後に自動明度補正を追加し、DNGでは線形値と露出指示を保存します。

## 対応範囲

| 対象 | 扱い |
| --- | --- |
| 2×2 RGB Bayer | rawpy／LibRawが読み込めるもの。RGGB・BGGR・GRBG・GBRG |
| X-Trans・Foveon・モノクロRAW・非RGB CFA | 非対応として拒否 |
| 既にRGB化されたDNG | 入力として非対応 |
| 奇数寸法 | 右端1列／下端1行を切り捨て |
| 縦位置 | センサー配列の向きで保存。自動回転なし |
| EXIF／ICC | 元RAWのメタデータは転記しない |

この出力は厳密な測光的輝度Yやモノクロ専用センサーの信号とは異なります。
デモザイクを省いてもモアレやエイリアシングが完全に消えるわけではありません。
黒補正はrawpyの色別定数のみで、行・列補正は行いません。
可視領域を使うため通常現像の画像寸法と異なる場合があります。元RAWは変更しません。

## 検証

```bash
python -m unittest -v test_converter.py
```

合成データで2×2平均、奇数寸法、従来の線形出力、自動明度、単調な明部、入力／出力保護を確認しています。
複数タイルの16bit DNGを可逆圧縮・無圧縮で保存し、tifffileとLibRawの両方で画素の完全一致を検証します。

実RAWはCanon EOS RのCR3 2枚とSony α1のARW 1枚で検証しました。
各TIFFの平均明度が50%に近いことと、DNGの保存画素が線形平均の16bit値を保持することを確認しています。
Lightroom／Camera RawおよびWindows BATの実機操作は未検証です。

## 参照

- [rawpy API](https://letmaik.github.io/rawpy/api/rawpy.RawPy.html)
- [tifffile](https://github.com/cgohlke/tifffile)
- [imagecodecs](https://github.com/cgohlke/imagecodecs)
- [Adobe DNG仕様](https://helpx.adobe.com/camera-raw/desktop/dng-and-file-formats/digital-negative.html)
