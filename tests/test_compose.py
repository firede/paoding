"""References composed for characters MMAH lacks."""

import numpy as np

from paoding import compose
from paoding.compose import Composer
from paoding.mmah import MMAH
from paoding.segment import segment

from test_core import outline_font, require_file  # noqa: F401  (fixture)


def bar(x0, y0, x1, y1):
    """A stroke as MMAH draws one: outline and median."""
    return f"M {x0} {y0} L {x1} {y0} L {x1} {y1} L {x0} {y1} Z", [[x0, (y0 + y1) / 2], [x1, (y0 + y1) / 2]]


def entry(char, decomposition, parts):
    """parts: per part, the boxes of its strokes."""
    matches, strokes, medians = [], [], []
    for k, boxes in enumerate(parts):
        for b in boxes:
            s, m = bar(*b)
            matches.append([k]); strokes.append(s); medians.append(m)
    return ({"character": char, "decomposition": decomposition, "matches": matches},
            {"character": char, "strokes": strokes, "medians": medians})


def composer(*entries, table):
    return Composer({d["character"]: d for d, _ in entries}, {g["character"]: g for _, g in entries}, table)


def test_parts_come_from_where_mmah_draws_them():
    ming = entry("明", "⿰日月", [[(100, 300, 380, 340), (100, 500, 380, 540)], [(500, 100, 900, 140)]])
    tang = entry("汤", "⿰氵昜", [[(80, 100, 200, 140)], [(400, 200, 920, 240), (400, 600, 920, 640)]])
    # MMAH calls 汤's right 昜; the table calls it 𠃓, as it does in 旸
    c = composer(ming, tang, table={"汤": "⿰氵𠃓", "旸": "⿰日𠃓"})
    d, g = c.entry("旸")
    assert d["decomposition"] == "⿰日𠃓"
    assert d["matches"] == [[0], [0], [1], [1]]
    left, right = np.array(g["medians"][:2]), np.array(g["medians"][2:])
    # 日 from 明 and 𠃓 from 汤 stood apart (380 to 400): they meet halfway, a gap between them
    assert left[:, :, 0].max() == 380 and right[:, :, 0].min() == 400
    assert np.allclose(left[:, :, 1], [[320, 320], [520, 520]])


def test_overlapping_parts_meet_halfway():
    ming = entry("明", "⿰日月", [[(100, 300, 600, 340)], [(500, 100, 900, 140)]])
    tang = entry("汤", "⿰氵昜", [[(80, 100, 200, 140)], [(300, 200, 920, 240)]])
    d, g = composer(ming, tang, table={"旸": "⿰日昜"}).entry("旸")
    (left,), (right,) = np.array(g["medians"][:1]), np.array(g["medians"][1:])
    # 日 reached 600 and 昜 began at 300: both give way to the middle, 450
    assert left[:, 0].max() == 440 and right[:, 0].min() == 460


def test_parts_without_strokes_give_nothing():
    ming = entry("明", "⿰日月", [[(100, 300, 380, 340)], [(500, 100, 900, 140)]])
    c = composer(ming, table={"晘": "⿰日{3}", "朙": "⿻日月", "旦": "⿱日一"})
    assert c.entry("晘") is None          # an unencoded part
    assert c.entry("朙") is None          # interwoven
    assert c.entry("昍") is None          # not in the table
    assert c.entry("旦") is None          # no character here has one part above another


def test_composed_reference_segments(outline_font):
    table = compose.read_ids(require_file("data/ids/IDS.TXT"))
    mmah = MMAH(require_file("data/mmah/dictionary.txt").parent, None, table)
    assert "玕" not in mmah.chars() and "玕" in mmah
    ref = mmah.reference("玕")
    assert ref.composed and ref.ids == "⿰𤣩干" and set(ref.sources) == {"composed"}
    seg = segment("玕", outline_font, mmah)
    assert seg.composed and seg.status == "auto"
