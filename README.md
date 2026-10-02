# pyramid_tool — 本地图像金字塔与切片生成工具

一个纯本地的图像金字塔（image pyramid）与 tile 切片工具。所有输入图片、
缩放层、tile、manifest 和缓存**只存储在本地文件或内存中**，不依赖地图
服务器、CDN、云存储或任何外部服务。

## 安装与依赖

- Python >= 3.9，依赖 `numpy`、`Pillow`（测试需要 `pytest`）
- 支持读取本地 PNG / JPEG（`.png` / `.jpg` / `.jpeg`），统一转换为 RGB

## 使用方法

```bash
python -m pyramid_tool build <图片路径> -o <输出目录> \
    [--tile-size 256] [--min-size 256] \
    [--resample nearest|bilinear] [--edge-mode pad|crop]
```

输出目录结构：

```
out/
  manifest.json
  tiles/<level>/<x>_<y>.png
```

## 层级算法

- level 0 为原图；每向上一层，宽和高分别做 `ceil(d / 2)`（最小 1 px）。
- 当 `max(width, height) <= min_size` 时停止生成新层。
- 例：773x501、`min_size=100` → 773x501 → 387x251 → 194x126 → 97x63，共 4 层。

## 缩放方式

- `nearest`：最近邻；`bilinear`：双线性。
- 两者均采用 align-centers 约定：`src = (dst + 0.5) * (src_len / dst_len) - 0.5`，
  基于 numpy float64 实现并四舍五入到 uint8，**相同输入必得到位级一致的结果**，
  不随 Pillow 版本变化。

## 坐标与边缘规则

- tile 坐标 `(x, y)` 为 0 起始的列、行号（原点在左上角），覆盖像素范围
  `[x*tile_size, (x+1)*tile_size) × [y*tile_size, (y+1)*tile_size)`。
- 每层 tile 数为 `ceil(w/tile_size) × ceil(h/tile_size)`。
- 边缘不足完整 tile 的区域有两种明确规则（`--edge-mode`）：
  - `pad`（默认）：所有 tile 均为完整 `tile_size × tile_size`，图外区域
    以 `pad_color`（默认黑色）填充；manifest 中的 `content_width/height`
    记录有效内容尺寸。
  - `crop`：边缘 tile 按实际大小裁剪存储，manifest 中的 `width/height`
    即为真实尺寸。

## manifest.json

记录校验与增量重建所需的全部信息：

- `source`：源图路径、尺寸、文件内容的 SHA-256。
- `config`：tile_size、min_size、resample、edge_mode、pad_color。
- `levels[]`：每层的 `level`、`width`、`height`、`tiles_x/y`，以及每个
  tile 的 `x`、`y`、存储尺寸 `width/height`、有效内容尺寸
  `content_width/height`、相对路径 `file` 和文件内容的 `sha256`。

## 增量重建与原子写入

- 重建时先比对源图哈希与配置：均一致才进入增量模式。
- 增量模式下逐个校验 manifest 中记录的 tile 文件哈希；**未变化的输入
  不会重写任何 tile**；缺失或损坏（哈希不符）的 tile 会被单独重新生成。
- 配置或源图变化时自动全量重建。
- 所有文件（tile 与 manifest）都通过「同目录临时文件 + `os.replace`」
  原子写入；manifest 最后提交，因此生成中断绝不会留下被 manifest 误认
  为有效的半文件。下次构建开始时还会清理上次中断残留的 `*.tmp-*` 文件。

## 运行测试

```bash
python -m pytest -v
```

测试在终端内完成并输出校验结果，不会打开任何图片窗口。覆盖场景：
奇数尺寸层级、边缘 tile（pad/crop）、nearest/bilinear 正确性与确定性、
增量构建（无变化零写入）、tile 损坏/缺失后的定点重生成、中途失败
（manifest 不提交、无残留临时文件、可恢复）以及配置变化触发全量重建。
