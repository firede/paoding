# 数据来源与许可

paoding 的代码采用 MIT 许可。输入数据不在仓库里，由 `paoding fetch` 按 `sources.toml` 下载固定版本并核对哈希。

| 输入 | 用途 | 许可 |
|---|---|---|
| [Make Me a Hanzi](https://github.com/skishore/makemeahanzi) `dictionary.txt` | 拆法、笔画归属 | LGPL-3.0 |
| Make Me a Hanzi `graphics.txt` | 参照笔画 | Arphic Public License |
| [BabelStone IDS](https://babelstone.co.uk/CJK/IDS.HTML) `IDS.TXT` | MMAH 没有的字的拆法 | 维护者声明不保留权利 |
| [Unihan](https://www.unicode.org/charts/unihan.html) | 《通用规范汉字表》字表（`kTGH`） | Unicode License v3 |
| 思源黑体、思源宋体 | 目标字体 | SIL OFL 1.1 |
| Unifont、缝合像素字体 | 位图目标字体 | 见 `sources.toml` |

## 发布生成的标注

- 记录中的 `ids`、`nodes` 和节点路径源自 MMAH `dictionary.txt`（经展开和修正），这部分按 LGPL-3.0 提供，请随数据集附上说明。带 `"composed": true` 的记录，`ids` 来自 BabelStone 的 IDS 表。
- 切线位置和面的标签借助 MMAH 的 `graphics.txt` 计算，但记录中不含它的任何轮廓或中线。
- 轮廓序号和坐标描述的是目标字体，由它重建出的任何字形都受该字体许可约束。例如思源黑体是 OFL，由它衍生的字体不能使用保留名 “Source”。
- `paoding run` 的报告图里画有由 MMAH 几何变形而来的参考笔画，不要当作数据发布。
