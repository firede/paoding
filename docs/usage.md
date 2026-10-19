# 使用

```sh
uv sync
uv run paoding fetch        # 按 sources.toml 下载固定版本的输入到 data/
```

## 切分

```sh
uv run paoding run --font data/fonts/SourceHanSansSC-Regular.otf --out out/sans --jobs 8
```

默认跑 Make Me a Hanzi（MMAH）收录的 9574 字，加上《通用规范汉字表》中 MMAH 没有的字。只跑部分字：`--chars 字串`，或 `--testset 文件`（`paoding testset` 生成一份 200 字的测试集）。

`run` 写出：

- `records.jsonl`：标注，格式见[标注格式](format.md)；
- `report/sheet-*.png`：对照图，按置信度从低到高排列；
- `report/summary.md`、`report/low.tsv`：汇总和低置信清单。

对照图里画有由 MMAH 几何变形而来的参考笔画，不要当作数据发布。

`--level N` 控制切分深度：第 1 层是顶层拆分，更深的层按部件自己的拆法继续展开，到基础部件为止。

**位图字体**：`--font` 传入 BDF、Unifont `.hex` 或 OpenType 位图（`.otb`）；像素轮廓的 OTF/TTF 还需要 `--ppem`。16px 效果好，12px 需要复核的字明显更多。

## 审阅

```sh
uv run paoding review out/sans out/sans-l2 --font data/fonts/SourceHanSansSC-Regular.otf
```

在 <http://127.0.0.1:8417/> 起一个本地页面，可同时载入几次运行并切换。字形由 `records.jsonl` 和字体重建，和下游拿到的几何一致。

- **字**：所有字的网格。可按置信度、常用字优先、Unicode 或随机抽样排序，按状态、结构筛选，或粘贴一串字只看这些。
- **部件**：同一部件在各字中切出来的样子排在一起，按分数从低到高。同一部件的错误往往成批出现。
- **缺口**：字体中没有切分记录的汉字，及其原因。
- **分组**：把选中的内容存为分组，之后可在任一次运行中查看，或与另一次运行并排对比。存在 `out/review-groups.json`（`--groups` 可改）。

“标记”显示结构检查标出的可疑位置；“骨架”画出目标字形的骨架、交点、拐角和收腰，即结构检查的依据。

点击字或部件选中，在右侧大图上点击可标记位置。选中的内容可复制成一段文本，含字体、运行、各字状态和复现用的 `inspect` 命令。重跑到同一目录后，页面回到前台时自动重新载入。

## 诊断

```sh
uv run paoding inspect --font data/fonts/SourceHanSansSC-Regular.otf --run out/sans 幻 啟@500,380
```

带调试信息重跑这几个字（`字@x,y` 附带一个字体单位的位置），写到 `out/inspect/<字>/`：

- `panels.png`：配准、拍平和切线、叠加，以及每个部件单独一格；
- `inspect.json`：各部件的分数、每一笔的归属和来源、各单元和面的归属，标记位置附近的情况，以及与运行记录是否一致。

给了 `--run` 时，层级、阈值和是否使用自带修正都取自那次运行；它用过的其他覆盖文件需要再用 `--overrides` 传一次。

## 修正

paoding 自带一份对 MMAH 结构数据的修正（`src/paoding/structure.toml`），默认加载，`--no-builtin-overrides` 关闭。

切错的地方可以用覆盖文件修正（`--overrides x.toml`，可重复，后加载的优先）：

- `[structure."<字>"]`：对所有字体生效的拆法或笔画归属；
- `[glyph."<字>"]`：只对某一套字体生效的单元归属、叠放顺序或审核状态。

格式见 `src/paoding/overrides.py`。
