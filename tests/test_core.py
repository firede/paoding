"""Core contracts, with optional real-data integration checks."""

import itertools
import json
from pathlib import Path

import numpy as np
import pathops
import pytest
import shapely
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen

from paoding import cli, ids, overrides, record, target, testset
from paoding.geometry import Canvas, area, from_shapely, op, resample, to_shapely
from paoding.mmah import MMAH
from paoding.register import Affine, RBFField, Warp, fit_box, ink_box
from paoding.segment import _cells, realize, segment
from paoding.segment_bitmap import _mend_runs, pixel_record, segment_bitmap
from paoding.target import BitmapTarget
from paoding.target.outline import OutlineFont


ROOT = Path(__file__).resolve().parents[1]


def require_file(relative):
    path = ROOT / relative
    if not path.is_file():
        pytest.skip(f"Missing optional input: {path}")
    return path


@pytest.fixture(scope="session")
def mmah():
    dictionary = require_file("data/mmah/dictionary.txt")
    require_file("data/mmah/graphics.txt")
    return MMAH(dictionary.parent)


@pytest.fixture(scope="session")
def outline_font():
    font = OutlineFont(require_file("data/fonts/SourceHanSansSC-Regular.otf"))
    yield font
    font.font.close()


@pytest.fixture(scope="session")
def bitmap_font():
    return BitmapTarget(require_file("data/fonts/unifont/unifont-17.0.05.hex.gz"))


# 克路希勤咋方夕: cut units whose cut lines once failed to separate the faces
OUTLINE_CHARS = "申山我京好国克路希勤咋方夕"


@pytest.fixture(scope="session")
def outline_segments(outline_font, mmah):
    return {char: segment(char, outline_font, mmah) for char in OUTLINE_CHARS}


@pytest.fixture(scope="session", params=list(OUTLINE_CHARS))
def outline_seg(request, outline_segments):
    return outline_segments[request.param]


@pytest.mark.parametrize("text, tree, leaves, depth", [
    ("女", "女", "女", 0),
    ("⿰女子", ("⿰", ["女", "子"]), "女子", 1),
    ("⿲木木木", ("⿲", ["木", "木", "木"]), "木木木", 1),
    ("⿳木木木", ("⿳", ["木", "木", "木"]), "木木木", 1),
    ("⿱⿰？？一", ("⿱", [("⿰", ["？", "？"]), "一"]), "？？一", 2),
])
def test_ids_parse(text, tree, leaves, depth):
    parsed = ids.parse(text)
    assert parsed == tree
    assert ids.leaves(parsed) == leaves
    assert ids.depth(parsed) == depth
    assert ids.is_leaf(parsed) == (depth == 0)
    assert ids.subtree(parsed, ()) == parsed


def test_ids_nested_subtree():
    tree = ids.parse("⿱⿰女子木")
    assert ids.subtree(tree, (0, 1)) == "子"
    assert ids.subtree(tree, (1,)) == "木"


@pytest.mark.parametrize("text", ["", "⿰女", "⿲木木", "⿱⿰？"])
def test_ids_truncated(text):
    with pytest.raises(ValueError, match="truncated"):
        ids.parse(text)


@pytest.mark.parametrize("text", ["女子", "⿰女子木", "⿲木木木木"])
def test_ids_trailing(text):
    with pytest.raises(ValueError, match="trailing"):
        ids.parse(text)


@pytest.mark.parametrize("path, text", [((), ""), ((0,), "0"), ((1, 0), "1.0"),
                                       ((0, 2, 1), "0.2.1")])
def test_ids_paths_round_trip(path, text):
    assert ids.fmt(path) == text
    assert ids.unfmt(text) == path
    assert ids.unfmt(ids.fmt(path)) == path
    assert ids.fmt(ids.unfmt(text)) == text


def test_reference_hao(mmah):
    ref = mmah.reference("好")
    assert ref.ids == "⿰女子"
    assert ref.paths == [(0,)] * 3 + [(1,)] * 3
    assert ref.sources == ["matches"] * 6
    assert len(ref.strokes) == len(ref.medians) == 6
    assert not ref.issues


@pytest.mark.parametrize("char, decomposition", [("司", "⿹？口"), ("丝", "⿱⿰？？一")])
def test_reference_unknown_leaves(mmah, char, decomposition):
    ref = mmah.reference(char)
    assert ref.ids == decomposition
    matches = mmah.dictionary[char]["matches"]
    nulls = [i for i, match in enumerate(matches) if match is None]
    assert nulls, "Fixture must exercise unmatched strokes"
    for i in nulls:
        assert ref.paths[i] == (0,)
        assert ref.sources[i] == "unknown-leaf"


def test_reference_missing_part_issue(mmah):
    ref = mmah.reference("才")
    assert any(issue.startswith("part 1 ") and issue.endswith("has no strokes")
               for issue in ref.issues)


def test_reference_nodes_keep_short_paths(mmah):
    ref = mmah.reference("好")
    assert ref.nodes(0) == ([()], [0] * 6)
    assert ref.nodes(3) == ([(0,), (1,)], [0, 0, 0, 1, 1, 1])


def test_reference_structure_override(mmah):
    original = mmah.reference("好")
    matches = [[0], [0], [0], [1, 0], [1, 1], [1, 1]]
    ref = mmah.reference("好", {"ids": "⿰女⿱子子", "matches": matches})
    assert ref.ids == "⿰女⿱子子"
    assert ref.tree == ids.parse(ref.ids)
    assert ref.paths == [tuple(p) for p in matches]
    assert ref.sources == ["override"] * 6
    assert ref.nodes(3) == ([(0,), (1, 0), (1, 1)], [0, 0, 0, 1, 2, 2])
    assert mmah.dictionary["好"]["decomposition"] == original.ids
    for before, after in zip(original.strokes, ref.strokes):
        np.testing.assert_array_equal(before, after)


def test_reference_expands_components(mmah):
    assert mmah.reference("照").ids == "⿱昭灬"
    ref = mmah.reference("照", depth=3)
    assert ref.ids == "⿱⿰日⿱刀口灬"
    assert ref.tree[1][1] == "灬"
    assert ref.nodes(1)[0] == [(0,), (1,)]
    assert set(ref.paths) == {(0, 0), (0, 1, 0), (0, 1, 1), (1,)}
    # 日 is a basic component: MMAH's ⿴口一 is not followed
    assert all(len(p) <= 3 for p in ref.paths)


def test_reference_basic_components(mmah):
    assert all(mmah.basic(c) for c in "口木日氵王贝")
    assert not any(mmah.basic(c) for c in "召昭吂")


def test_reference_unknown_parts_kept_whole(mmah):
    # 門 is ⿰？？ in MMAH: no named part, so nothing to split by
    ref = mmah.reference("門")
    assert ids.is_leaf(ref.tree) and ref.ids == ids.UNKNOWN
    assert set(ref.paths) == {()} and not ref.issues and ref.notes


def test_level_two_nested_split(outline_font, mmah):
    seg = segment("圈", outline_font, mmah, level=2)
    assert [ids.fmt(n) for n in seg.nodes] == ["0", "1.0", "1.1"]
    pieces = seg.pieces("flat")
    assert all(area(p) > 1000 for p in pieces.values())
    assert seg.status == "auto"



def test_level_two_hairline_faces(outline_font, mmah):
    # two hairline faces in 麩 collapse when their union is snapped to the grid
    seg = segment("麩", outline_font, mmah, level=2)
    for unit in seg.units:
        if unit.method == "cut":
            assert all(area(p) > 0 for p in realize(unit.path, unit.cuts, unit.faces))

def test_outline_units_guo(outline_font):
    units = outline_font.units("国")
    originals = outline_font.contours("国")
    assert len(units) == 3
    indices = [i for unit in units for i in unit.contours]
    assert sorted(indices) == list(range(len(originals)))
    assert len(indices) == len(set(indices))
    # The enclosing 囗 is the unit with the largest bounding rectangle.
    enclosing = max(units, key=lambda u: shapely.box(*u.path.bounds).area)
    assert len(enclosing.contours) == 2
    contours = list(enclosing.path.contours)
    assert len(contours) == 2
    assert sorted(c.clockwise for c in contours) == [False, True]


def test_overlapping_outline_contours_form_one_unit(tmp_path):
    builder = FontBuilder(1000, isTTF=False)
    builder.setupGlyphOrder([".notdef", "overlap"])
    builder.setupCharacterMap({ord("合"): "overlap"})
    empty = T2CharStringPen(600, None)
    pen = T2CharStringPen(600, None)
    for x0, y0, x1, y1 in [(0, 0, 200, 200), (100, 100, 300, 300)]:
        pen.moveTo((x0, y0))
        for point in [(x1, y0), (x1, y1), (x0, y1)]:
            pen.lineTo(point)
        pen.closePath()
    builder.setupHorizontalMetrics({name: (600, 0) for name in [".notdef", "overlap"]})
    builder.setupHorizontalHeader(ascent=880, descent=-120)
    builder.setupNameTable({"familyName": "Core Fixture", "styleName": "Regular"})
    builder.setupOS2(sTypoAscender=880, sTypoDescender=-120)
    builder.setupPost()
    builder.setupCFF("CoreFixture", {}, {".notdef": empty.getCharString(),
                                         "overlap": pen.getCharString()}, {})
    path = tmp_path / "overlap.otf"
    builder.save(path)
    font = OutlineFont(path)
    try:
        units = font.units("合")
        assert len(units) == 1
        assert units[0].contours == [0, 1]
        assert area(units[0].path) == pytest.approx(70000)
    finally:
        font.font.close()


def glyph_area(font, char):
    raw = pathops.Path()
    font.glyphs[font.glyph_name(char)].draw(raw.getPen())
    return area(pathops.simplify(raw, fix_winding=True)) * font.scale ** 2


def test_flat_pieces_tile_glyph(outline_seg, outline_font):
    pieces = list(outline_seg.pieces("flat").values())
    expected = glyph_area(outline_font, outline_seg.char)
    assert expected > 0
    assert sum(area(p) for p in pieces) == pytest.approx(expected, rel=0.005)
    for a, b in itertools.combinations(pieces, 2):
        assert area(op(a, b, "INTERSECTION")) <= max(0.01, expected * 1e-6)


def test_overlay_contains_flat(outline_seg):
    flat, overlay = outline_seg.pieces("flat"), outline_seg.pieces("overlay")
    assert set(flat) == set(overlay) == set(outline_seg.nodes)
    for node in outline_seg.nodes:
        assert area(overlay[node]) + 0.01 >= area(flat[node])
        assert area(op(flat[node], overlay[node], "DIFFERENCE")) <= 0.01


def test_cut_faces_all_realized(outline_seg):
    for unit in outline_seg.units:
        if unit.method != "cut":
            continue
        pieces = realize(unit.path, unit.cuts, unit.faces)
        assert all(area(p) > 0 for p in pieces), (outline_seg.char, [f.nodes for f in unit.faces])


# 喹 吟: the right part's left-falling stroke, longer in the reference, was
# pulled across the gap onto the 口; only its tail reaches that unit
@pytest.mark.parametrize("char", "喹吟")
def test_stroke_tail_spilt_onto_another_unit(outline_font, mmah, char):
    seg = segment(char, outline_font, mmah)
    assert any(u.method == "direct" and u.node == (0,) for u in seg.units), char
    assert seg.status == "auto"


# 兹 周: a face pinched in two where the outline nearly touches itself
@pytest.mark.parametrize("char", "兹周")
def test_every_rebuilt_cell_has_a_face_point(outline_font, mmah, char):
    seg = segment(char, outline_font, mmah)
    for unit in seg.units:
        if unit.method != "cut":
            continue
        pts = shapely.points(np.array([f.point for f in unit.faces]))
        cells = [c for c in _cells(to_shapely(unit.path, steps=16), unit.cuts) if c.area >= 1.0]
        assert all(shapely.contains(c, pts).any() for c in cells), char


def test_record_json_round_trip(outline_seg, outline_font):
    assert outline_font.units_per_em == 1000
    rec = json.loads(json.dumps(record.to_record(outline_seg, outline_font)))
    assert rec["char"] == outline_seg.char
    rebuilt = record.realize_record(rec, outline_font)
    for view in ("overlay", "flat"):
        expected = outline_seg.pieces(view)
        assert set(rebuilt[view]) == {ids.fmt(n) for n in expected}
        for node, piece in expected.items():
            assert area(rebuilt[view][ids.fmt(node)]) == pytest.approx(
                area(piece), rel=0.005, abs=0.5
            ), (outline_seg.char, view, node)


@pytest.mark.parametrize("char, rule, owner", [("申", "crossing", (1,)), ("山", "through", (0,))])
def test_shared_face_rules(outline_segments, char, rule, owner):
    seg = outline_segments[char]
    shared = [f for u in seg.units for f in u.faces if len(f.nodes) > 1]
    assert shared
    assert all(f.rule == rule and f.flat == owner for f in shared), shared


def test_overrides_load_merge_and_for_char(tmp_path):
    first, second = tmp_path / "first.toml", tmp_path / "second.toml"
    first.write_text('''[structure."好"]
ids = "⿰女子"
matches = [[0], [0], [0], [1], [1], [1]]
[glyph."好"]
stack = ["0", "1"]
units = { "0" = "1" }
status = "auto"
note = "retained metadata"
[glyph."申"]
status = "exclude"
''', encoding="utf-8")
    second.write_text('''[structure."好"]
ids = "⿱女子"
[glyph."好"]
stack = ["1", "0"]
status = "verified"
''', encoding="utf-8")
    loaded = overrides.load([first, second])
    expected = {"stack": ["1", "0"], "units": {"0": "1"}, "status": "verified",
                "structure": {"ids": "⿱女子", "matches": [[0]] * 3 + [[1]] * 3}}
    assert overrides.for_char(loaded, "好") == expected
    assert loaded["glyph"]["好"]["note"] == "retained metadata"
    assert overrides.for_char(loaded, "申") == {"status": "exclude"}
    assert overrides.for_char(loaded, "无") == {}
    assert overrides.load([]) == {"structure": {}, "glyph": {}}


def test_overrides_unknown_section(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('[typo]\nstatus = "verified"\n')
    with pytest.raises(ValueError, match="unknown sections.*typo"):
        overrides.load([path])


def test_segmentation_overrides(outline_font, mmah, outline_segments):
    first_unit = outline_font.units("好")[0]
    contour = first_unit.contours[0]
    # Force the first unit to the opposite part, so this tests a real reassignment.
    baseline = outline_segments["好"]
    before = next(u for u in baseline.units if contour in u.contours)
    assert before.method == "direct"
    forced_node = (1,) if before.node == (0,) else (0,)
    result = segment("好", outline_font, mmah, override={
        "units": {str(contour): ids.fmt(forced_node)},
        "stack": ["1", "0"], "status": "verified",
    })
    assert result.nodes == [(1,), (0,)]
    assert result.status == "verified"
    forced = next(u for u in result.units if contour in u.contours)
    assert forced.method == "override"
    assert forced.node == forced_node
    rec = record.to_record(result, outline_font)
    entry = next(u for u in rec["units"] if contour in u["contours"])
    assert entry["override"] is True
    assert entry["node"] == ids.fmt(forced_node)
    assert rec["status"] == "verified"
    assert rec["overrides"] == ["stack", "status", "units"]


def test_bitmap_segmentation_and_pixel_record(bitmap_font, mmah):
    seg = segment_bitmap("好", bitmap_font, mmah)
    grid = seg.grid
    ink = bitmap_font.bitmap("好").bitmap
    assert grid is not None
    assert grid.flat.shape == ink.shape
    assert grid.overlay.shape == (len(seg.nodes), *ink.shape)
    np.testing.assert_array_equal(grid.flat >= 0, ink)
    assert np.all(grid.flat[ink] < len(seg.nodes))
    assert not grid.overlay[:, ~ink].any()
    for k in range(len(seg.nodes)):
        assert np.all(grid.overlay[k][grid.flat == k])
    pixels = json.loads(json.dumps(pixel_record(seg, bitmap_font)))
    assert (pixels["left"], pixels["top"]) == (grid.glyph.left, grid.glyph.top)
    assert len(pixels["rows"]) == ink.shape[0]
    assert all(len(row) == ink.shape[1] for row in pixels["rows"])
    decoded = np.full(ink.shape, -1)
    overlay = np.zeros_like(grid.overlay)
    for r, row in enumerate(pixels["rows"]):
        for c, letter in enumerate(row):
            if letter == ".":
                continue
            entry = pixels["legend"][letter]
            decoded[r, c] = seg.nodes.index(ids.unfmt(entry["flat"]))
            for node in entry["nodes"]:
                overlay[seg.nodes.index(ids.unfmt(node)), r, c] = True
    np.testing.assert_array_equal(decoded, grid.flat)
    np.testing.assert_array_equal(overlay, grid.overlay)
    assert record.to_record(seg, bitmap_font)["pixels"] == pixels


def test_mend_runs():
    flat = np.full((5, 9), -1)
    flat[2, :] = 0
    flat[2, 3:5] = 1                 # a stretch of part 1 inside part 0's run
    flat[:, 7] = 1                   # part 1's vertical crossing the run
    out, broken = _mend_runs(flat)
    assert (out[2, :7] == 0).all() and out[2, 7] == 1 and out[2, 8] == 0
    assert broken == []
    flat[2, 3:5] = 0
    flat[2, :2] = 1                  # part 1 at the run's end: left, but reported
    out, broken = _mend_runs(flat)
    assert (out == flat).all() and broken == [(0, 1)]


def test_bitmap_runs(bitmap_font, mmah):
    # 目's 二 meets 口's sides: crossings, not part of the run to mend
    seg = segment_bitmap("目", bitmap_font, mmah)
    assert all(v["area"] > 0 for v in seg.metrics["nodes"].values())
    # 阜: the long stroke of 十 is split with 㠯
    assert segment_bitmap("阜", bitmap_font, mmah).status == "low"


def test_testset_round_trip(tmp_path):
    path = tmp_path / "set.tsv"
    rows = [("好", ["⿰", "common"]), ("司", ["inferred"]), ("輿", [])]
    testset.write(rows, path)
    assert path.read_text(encoding="utf-8").startswith("# char\ttags\n")
    assert testset.read(path) == [row[0] for row in rows]
    testset.write([], path)
    assert testset.read(path) == []


@pytest.mark.parametrize("char, level", [("啊", 1), ("亍", 2), ("輿", 0)])
def test_gb2312_level(char, level):
    assert testset.gb2312_level(char) == level


def test_canvas_coordinates_and_ink_box():
    canvas = Canvas(res=10)
    np.testing.assert_array_equal(canvas.to_px([(0, 880), (1000, -120)]), [(0, 0), (10, 10)])
    points = canvas.pixel_centers(np.array([0, 9]), np.array([0, 9]))
    rows, cols = canvas.index(points)
    np.testing.assert_allclose(rows, [0, 9])
    np.testing.assert_allclose(cols, [0, 9])
    mask = np.zeros(canvas.shape, bool)
    mask[2:5, 3:7] = True
    lo, hi = ink_box(mask, canvas)
    np.testing.assert_allclose(lo, [300, 380])
    np.testing.assert_allclose(hi, [700, 680])


def test_geometry_hole_round_trip_and_resample():
    ring = shapely.box(0, 0, 10, 10).difference(shapely.box(2, 2, 8, 8))
    path = from_shapely(ring)
    assert area(path) == pytest.approx(64)
    assert to_shapely(path).symmetric_difference(ring).area == pytest.approx(0)
    points = resample(np.array([[0, 0], [10, 0], [10, 10]]), step=5)
    np.testing.assert_allclose(points, [[0, 0], [5, 0], [10, 0], [10, 5], [10, 10]])


def test_registration_transforms_and_thin_box():
    points = np.array([[0., 0.], [10., 20.]])
    affine = Affine(2, 3, 4, -5)
    np.testing.assert_allclose(affine(points), [[4, -5], [24, 55]])
    field = RBFField(3)
    np.testing.assert_allclose(field(points), points)
    np.testing.assert_allclose(Warp(affine, [field])(points), affine(points))
    src = (np.array([0., 0.]), np.array([100., 200.]))
    dst = (np.array([10., -20.]), np.array([210., 580.]))
    np.testing.assert_allclose(fit_box(src, dst)(np.array(src)), np.array(dst))
    thin = (np.array([0., 0.]), np.array([0., 200.]))
    np.testing.assert_allclose(fit_box(thin, dst)(np.array(thin)), [[110, -20], [110, 580]])


def test_cli_run(tmp_path, monkeypatch, outline_font, mmah):
    # Reuse session inputs while exercising parsing, workers, and output writing.
    def cached_mmah(path, structures=None, table=None):
        assert Path(path).resolve() == ROOT / "data/mmah"
        return mmah

    def cached_font(path, ppem=None):
        assert Path(path) == Path(outline_font.path)
        assert ppem is None
        return outline_font

    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(cli, "MMAH", cached_mmah)
    monkeypatch.setattr(target, "open_target", cached_font)
    monkeypatch.setattr(cli, "_worker", {})
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["run", "--font", outline_font.path, "--chars", "好申",
                  "--out", str(tmp_path), "--no-report"])
    assert exit_info.value.code == 0
    rows = [json.loads(line) for line in (tmp_path / "records.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert rows[0]["kind"] == "header"
    assert rows[0]["font"]["file"] == Path(outline_font.path).name
    assert [r["char"] for r in rows[1:]] == ["好", "申"]
    assert all("units" in r and r["nodes"] for r in rows[1:])


def test_builtin_overrides(mmah):
    loaded = overrides.load([overrides.BUILTIN])["structure"]
    for char, entry in loaded.items():
        ref = mmah.reference(char, entry)
        assert not ref.issues, (char, ref.issues)
        assert len(ref.paths) == len(ref.strokes)
