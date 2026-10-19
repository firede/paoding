"""Units given whole to a part because the font draws them so elsewhere."""

import numpy as np
import pathops
from shapely.geometry import box

from paoding import library
from paoding.geometry import from_shapely
from paoding.cli import _shapes
from paoding.segment import segment

from test_core import mmah, outline_font  # noqa: F401  (fixtures)


def square(x0, y0, side):
    return from_shapely(box(x0, y0, x0 + side, y0 + side))


def test_like_needs_size_shape_and_enough_characters():
    lib = library.Library()
    lib.add("口", library.describe(square(100, 100, 300)), "吃")
    probe = library.describe(square(500, 400, 302))       # same shape elsewhere on the em
    assert lib.like("口", probe) == []                     # one character is not enough
    lib.add("口", library.describe(square(120, 80, 299)), "呢")
    assert lib.like("口", probe) == ["吃", "呢"]
    assert lib.like("日", probe) == []
    assert lib.like("口", library.describe(square(100, 100, 400))) == []     # too big


def test_cut_unit_matched_whole(outline_font, mmah):
    lib = library.Library()
    for c in "院隆陈":
        for _, kind, node, shape in _shapes(segment(c, outline_font, mmah)):
            if kind == "whole" and node == "0":
                lib.add("阝", shape, c)
    seg = segment("陽", outline_font, mmah)
    assert "split" in {f["kind"] for f in seg.flags}       # 阝's ear tail given to 昜
    (contour,) = [c for c, kind, node, shape in _shapes(seg)
                  if kind == "cut" and node == "0" and lib.like("阝", shape)]
    seg = segment("陽", outline_font, mmah, matched={contour: ((0,), ["院", "隆"])})
    (u,) = [u for u in seg.units if contour in u.contours]
    assert u.method == "match" and u.node == (0,) and u.like == ["院", "隆"]
    assert not seg.flags


def test_fit_finds_a_moved_template():
    ink = library.FIT.path_mask(square(300, 200, 260))
    share, placed = library.fit(ink, square(250, 240, 260))     # the template's own character has it elsewhere
    assert share > 0.97
    assert np.allclose(placed.bounds, (300, 200, 560, 460), atol=6)
    share, _ = library.fit(ink, square(250, 240, 400))          # too big to fit by scaling a little
    assert share < 0.9


def test_template_decides_a_touching_part(outline_font, mmah):
    source = segment("節", outline_font, mmah)
    part = pathops.Path()
    for u in source.units:
        if u.method == "direct" and u.node == (0,):
            u.path.draw(part.getPen())
    seg = segment("等", outline_font, mmah)
    assert "split" in {f["kind"] for f in seg.flags}            # a stroke of ⺮ cut where it touches 寺
    seg = segment("等", outline_font, mmah, templates={(0,): [("節", part)]})
    assert seg.templates == {(0,): "節"} and not seg.flags
