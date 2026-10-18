# paoding

把任意字体里的汉字，按表意文字描述序列（IDS）切分成部件：可以给部件分别上色、按部件检索、逐个部件展示，而结果不绑定于结构数据所依据的那套字体。

[Make Me a Hanzi](https://github.com/skishore/makemeahanzi)（MMAH）记录了每个字怎么拆、每一笔属于哪个部件，但它的笔画几何来自一套楷体。paoding 把这些笔画配准到目标字形上，再据此切分目标字体**自己的**轮廓（或像素）。产物是对目标字体的标注；MMAH 的几何只用作参照，不会写进产物。

状态：早期。矢量字体在第一层及更深层（`--level 2` 起）都切得不错；位图字体在 16px（Unifont）上效果好，12px 下需要人工复核的字明显更多。

## 使用

```sh
uv sync
uv run paoding fetch                     # 按 sources.toml 下载固定版本的输入到 data/
uv run paoding testset --font data/fonts/SourceHanSansSC-Regular.otf
uv run paoding run --font data/fonts/SourceHanSansSC-Regular.otf \
    --testset testsets/core200.tsv --out out/core200 --jobs 8
```

`run` 写出 `records.jsonl`（格式见 [docs/format.md](docs/format.md)）和一份报告：

- `report/sheet-*.png`：对照图，按置信度从低到高排列；
- `summary.md` 和 `low.tsv`：汇总和低置信清单。

对照图里画有配准后的参考笔画（由 MMAH 几何变形而来），不要把报告当作数据发布。

### 审阅

```sh
uv run paoding review out/sans-l1 out/sans-l2 --font data/fonts/SourceHanSansSC-Regular.otf
```

在本地起一个页面（默认 <http://127.0.0.1:8417/>），可同时载入几次运行并切换。字形由 `records.jsonl` 和字体经 `realize_record` 重建，和下游拿到的几何完全一致。

- **字**：所有字的网格，可按置信度、常用字优先（GB 2312 一级、二级、其余）、Unicode 或随机抽样排序，按状态、结构筛选，或粘贴一串字只看这些。
- **部件**：同一部件在各字中切出来的样子排在一起，按部件分数从低到高排。同一部件的错误往往成批出现，在这里最容易看出来。
- **缺口**：字体中没有切分记录的汉字，分为 MMAH 没有此字的、和有但不在本次运行中的。

审阅只需要“看”和“选”：点击字或部件选中，在右侧大图上点击可以标记不对劲的位置（字体单位）。选中的内容可以一键复制成一段文本，里面写明字体、运行、各字的状态，以及复现用的 `inspect` 命令，可以直接交给 agent 分析。选择保存在浏览器里，重启服务后仍在。

### 诊断

```sh
uv run paoding inspect --font data/fonts/SourceHanSansSC-Regular.otf --run out/sans-l1 幻 啟@500,380
```

带调试信息重跑这几个字（`字@x,y` 附带一个字体单位的位置），在 `out/inspect/<字>/` 下写出：

- `panels.png`：配准（按笔画编号的参照中线叠在目标墨迹上）、拍平和切线、叠加，以及每个部件单独一格；每 100 个字体单位一条网格线，标记的位置画圈；
- `inspect.json`：各部件的分数、每一笔的归属和来源、各单元和面的归属，标记位置落在哪个单元和面上、离哪几笔最近，以及结果是否与运行中的记录一致。

给了 `--run` 时，层级、阈值和是否使用自带修正都取自那次运行；它用过的其他覆盖文件需要用 `--overrides` 再传一次。

`--level N` 控制切分深度：第 1 层是顶层拆分，更深的层把部件按它们自己在 MMAH 中的拆法继续展开，到基础部件为止。

**位图字体**：`--font` 传入 BDF、Unifont `.hex` 或 OpenType 位图（`.otb`）文件；像素轮廓的 OTF/TTF 还需要传 `--ppem`。

## 修正

paoding 自带一份对 MMAH 结构数据的修正（`src/paoding/structure.toml`），运行时默认加载；用 `--no-builtin-overrides` 可以关闭。

工具切错的地方，可以用覆盖文件修正（`--overrides x.toml`，可重复，后加载的优先）：

- `[structure."<字>"]`：对所有字体生效的拆法或笔画归属；
- `[glyph."<字>"]`：只对某一套字体生效的单元归属、叠放顺序或审核状态。

格式见 `src/paoding/overrides.py`。

## 原理

1. **参照**：MMAH 给出拆法、每一笔所属部件的路径，以及笔画轮廓和中线。更深的层级里，部件按它们自己的条目继续展开。
2. **配准**：先把参照的墨迹外框对齐到目标的外框，再用一个平滑的非刚性形变，把中线拉到目标的骨架上（按位置和笔画方向匹配，由粗到细）。之后逐层把每个部件单独再配准一次：先对它覆盖的墨迹做仿射拟合，再做一次更温和的形变，这样部件保持自身形状，笔画也不会被拖到邻近部件的墨迹上。
3. **归属**：目标字形的墨迹分成若干单元（一条外轮廓连同它的孔洞）。只有一个部件的中线穿过的单元，整个归这个部件；否则沿配准后笔画周围的条带切开。部件相交处的面是共有的，拍平视图把它交给穿过交接处的那一笔，交叉时交给叠放在上层的部件。位图字体则逐个像素归属，并把被打断的横竖像素串修补回同一个部件。
4. **评分**：对每个部件，看它的中线有多少落在自己的墨迹上，以及它的墨迹有多少明显更靠近别的部件的笔画（位图字体看它的像素串有多少被别的部件截断）。置信度取最弱的部件，低于阈值的字标为 `low`，留待人工复核。

## 许可

代码采用 MIT 许可。输入数据各有许可，见 `sources.toml`：

- MMAH 的 `dictionary.txt`：LGPL-3.0；
- MMAH 的 `graphics.txt`：Arphic Public License；
- 思源黑体等字体：SIL OFL 1.1。

发布由 paoding 生成的数据集时请注意：

- 记录中的 `ids` 和节点路径源自 MMAH `dictionary.txt`（经展开和修正），这部分按 LGPL-3.0 提供，请随数据集附上说明；
- 记录中的轮廓序号和坐标描述的是目标字体，由它重建出的任何字形都受该字体许可约束。例如思源黑体是 OFL，由它衍生的字体不能使用保留名 “Source”；
- 记录中不含 MMAH 的轮廓或中线。
