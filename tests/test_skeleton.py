"""The skeleton graph and the structure checks made on it."""

import numpy as np
import pytest

from paoding import skeleton
from paoding.geometry import Canvas
from paoding.segment import segment

from test_core import mmah, outline_font, outline_segments  # noqa: F401  (fixtures)


def boxes(*rects, res=512):
    """Boolean ink on a canvas from (x0, y0, x1, y1) rectangles in the em frame (y up)."""
    canvas = Canvas(res)
    contours = [np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]]) for x0, y0, x1, y1 in rects]
    return canvas.fill(contours, rule="union"), canvas


def test_crossing_is_one_joint():
    ink, canvas = boxes((100, 340, 900, 420), (460, 0, 540, 800))   # 十
    g = skeleton.graph(ink, canvas)
    assert len(g.joints) == 1 and g.joints[0].degree == 4
    assert np.allclose(g.joints[0].point, [500, 380], atol=12)
    assert len(g.segments) == 4 and all(s.ends.count(0) == 1 for s in g.segments)


def test_tee_and_free_bar():
    ink, canvas = boxes((100, 600, 900, 680), (460, 100, 540, 680), (100, 0, 900, 60))   # 丅 over a separate 一
    g = skeleton.graph(ink, canvas)
    assert [j.degree for j in g.joints] == [3]
    free = [s for s in g.segments if s.ends == (-1, -1)]
    assert len(free) == 1 and free[0].length > 700 and not free[0].closed and not free[0].bends


def test_corners_are_bends():
    ink, canvas = boxes((100, 100, 180, 700), (100, 620, 800, 700))   # ㇕ turned: one stroke turning
    (s,) = skeleton.graph(ink, canvas).segments
    assert len(s.bends) == 1 and np.allclose(s.points[s.bends[0]], [140, 660], atol=25)
    ink, canvas = boxes((100, 100, 180, 700), (720, 100, 800, 700), (100, 100, 800, 180), (100, 620, 800, 700))  # 口
    g = skeleton.graph(ink, canvas)
    assert not g.joints and len(g.segments) == 1 and g.segments[0].closed and len(g.segments[0].bends) >= 3


def test_split_stretch_is_disputed():
    ink, canvas = boxes((100, 340, 900, 420))   # one bar, owned half by each of two parts
    g = skeleton.graph(ink, canvas)
    labels = np.where(ink, 0, -1)
    labels[:, 256:] = np.where(ink[:, 256:], 1, -1)
    spans = skeleton.stretches(g)
    own = skeleton.owned(g, spans, labels, canvas)
    assert [d["kind"] for d in skeleton.disputes(g, spans, own, [{} for _ in spans])] == ["split"]
    # medians running along it for part 1 while part 0 owns it all: a vote dispute
    labels = np.where(ink, 0, -1)
    own = skeleton.owned(g, spans, labels, canvas)
    line = np.column_stack([np.linspace(120, 880, 200), np.full(200, 380.0)])
    votes = skeleton.voted(g, spans, [line], [1])
    (d,) = skeleton.disputes(g, spans, own, votes)
    assert d["kind"] == "vote" and list(d["vote"]) == [1] and list(d["ink"]) == [0]


def test_stray_island():
    ink, canvas = boxes((100, 340, 900, 420), (100, 0, 900, 80))
    left = ink.copy()
    left[:, 256:] = False                    # part 0: the left of both bars, part 1: the right
    right = ink & ~left
    island = np.zeros_like(ink)
    island[:, 440:470] = ink[:, 440:470]     # part 0's overlay reaching into part 1's bars
    labels = np.where(left, 0, np.where(right, 1, -1))
    strays = skeleton.strays({0: left | island, 1: right}, labels, ink, canvas)
    assert len(strays) == 2 and {d["owner"] for d in strays} == {0}
    # the same islands owned by part 0 in the flat view too are whole strokes of its own
    labels = np.where(left | island, 0, labels)
    assert not skeleton.strays({0: left | island, 1: right & ~island}, labels, ink, canvas)


@pytest.fixture(scope="module")
def checked(outline_font, mmah):
    return {c: segment(c, outline_font, mmah) for c in "陽彙古"}


def test_structure_flags(checked, outline_segments):
    kinds = lambda c: sorted({f["kind"] for f in checked[c].flags})
    assert "split" in kinds("陽")                              # 阝's ear tail given to 昜
    assert "split" in kinds("彙")                              # 彑's long 横 cut in the middle
    assert not checked["古"].flags
    for c in "申山好国":
        assert not outline_segments[c].flags, c
    # a flag costs confidence
    seg = checked["陽"]
    assert seg.confidence < seg.metrics["confidence"]
