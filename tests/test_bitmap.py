"""Small on-disk fixtures exercise bit packing and baseline placement."""

import gzip
from pathlib import Path

import numpy as np
import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import newTable
from fontTools.ttLib.tables import E_B_L_C_ as eblc
from fontTools.ttLib.tables.BitmapGlyphMetrics import BigGlyphMetrics, SmallGlyphMetrics
from fontTools.ttLib.woff2 import haveBrotli

from paoding.target.bitmap import BitmapGlyph, open_bitmap_font


MASK = np.zeros((16, 8), dtype=bool)
MASK[1:6, 1:6] = True
MASK[2:5, 2:5] = False
MASK[14:16, 1:3] = True
WIDE = np.pad(MASK, ((0, 0), (0, 8)))
WIDE[8, 15] = True


def hex_bits(bitmap):
    return np.packbits(bitmap, axis=1).tobytes().hex().upper()


def bdf_text():
    rows = "\n".join(hex_bits(row[None, :]) for row in MASK)
    return f"""STARTFONT 2.1
FONT test
SIZE 16 72 72
FONTBOUNDINGBOX 16 16 0 -2
STARTPROPERTIES 3
PIXEL_SIZE 16
FONT_ASCENT 14
FONT_DESCENT 2
ENDPROPERTIES
CHARS 4
STARTCHAR A
ENCODING 65
DWIDTH 8 0
BBX 8 16 0 -2
BITMAP
{rows}
ENDCHAR
STARTCHAR negative-bearing
ENCODING 78
DWIDTH 10 0
BBX 9 3 -2 -1
BITMAP
8080
7F00
FF80
ENDCHAR
STARTCHAR space
ENCODING 32
DWIDTH 4 0
BBX 0 0 0 0
BITMAP
ENDCHAR
STARTCHAR unencoded
ENCODING -1 1234
DWIDTH 8 0
BBX 1 1 0 0
BITMAP
80
ENDCHAR
ENDFONT
"""


def write_bdf(tmp_path, suffix=".bdf", text=None):
    path = tmp_path / ("test" + suffix)
    data = (text if text is not None else bdf_text()).encode("ascii")
    path.write_bytes(gzip.compress(data) if suffix.endswith(".gz") else data)
    return path


def write_hex(tmp_path, suffix=".hex"):
    path = tmp_path / ("test" + suffix)
    data = (f"# fixture\n\n0041:{hex_bits(MASK)}\n"
            f"597D:{hex_bits(WIDE)}\n0020:{'00' * 16}\n"
            f"1F600:{hex_bits(MASK)}\n").encode("ascii")
    path.write_bytes(gzip.compress(data) if suffix.endswith(".gz") else data)
    return path


def rectangle(pen, x0, y0, x1, y1, reverse=False):
    points = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    if reverse:
        points.reverse()
    pen.moveTo(tuple(v * 100 for v in points[0]))
    for p in points[1:]:
        pen.lineTo(tuple(v * 100 for v in p))
    pen.closePath()


def outline_builder(is_ttf=False):
    builder = FontBuilder(1600, isTTF=is_ttf)
    names = [".notdef", "A", "N", "W", "space", "outlineOnly", "smile"]
    builder.setupGlyphOrder(names)
    builder.setupCharacterMap({65: "A", 78: "N", 87: "W", 32: "space",
                               90: "outlineOnly", 0x1F600: "smile"})
    glyphs = {}
    for name in names:
        pen = TTGlyphPen(None) if is_ttf else T2CharStringPen(800, None)
        if name in ("A", "smile"):
            rectangle(pen, 1, 8, 6, 13)
            rectangle(pen, 2, 9, 5, 12, reverse=True)
            rectangle(pen, 1, -2, 3, 0)
        elif name == "N":
            rectangle(pen, -2, -1, 1, 2)
        elif name == "W":
            rectangle(pen, 0, 0, 3, 3)
            rectangle(pen, 1, 1, 4, 4)
        glyphs[name] = pen.glyph() if is_ttf else pen.getCharString()
    bearings = {"A": 100, "smile": 100, "N": -200}
    builder.setupHorizontalMetrics({name: (800, bearings.get(name, 0))
                                    for name in names})
    builder.setupHorizontalHeader(ascent=1400, descent=-200)
    builder.setupNameTable({"familyName": "Bitmap Fixture", "styleName": "Regular"})
    builder.setupOS2(sTypoAscender=1300, sTypoDescender=-300,
                     usWinAscent=1400, usWinDescent=200)
    builder.setupPost()
    if is_ttf:
        builder.setupGlyf(glyphs)
        builder.setupMaxp()
    else:
        builder.setupCFF("BitmapFixture", {}, glyphs, {})
    return builder


def write_outline(tmp_path, suffix=".otf"):
    builder = outline_builder(is_ttf=suffix != ".otf")
    if suffix == ".woff2":
        if not haveBrotli:
            pytest.skip("WOFF2 requires fontTools' optional Brotli backend")
        builder.font.flavor = "woff2"
    path = tmp_path / ("outline" + suffix)
    builder.save(path)
    return path


def metrics(big=True, width=5, height=3, left=-2, top=2):
    m = BigGlyphMetrics() if big else SmallGlyphMetrics()
    m.width, m.height = width, height
    if big:
        m.horiBearingX, m.horiBearingY, m.horiAdvance = left, top, 8
        m.vertBearingX, m.vertBearingY, m.vertAdvance = 0, 0, 16
    else:
        m.BearingX, m.BearingY, m.Advance = left, top, 8
    return m


STRIKE_MASK = np.array([[1, 0, 0, 0, 1], [0, 1, 1, 1, 0], [1, 1, 0, 1, 1]], bool)


def write_otb(tmp_path, image_format=1, index_format=1, sizes=(16,), bit_depth=1,
              bitmap=STRIKE_MASK, left=-2, top=2):
    builder = outline_builder()
    font = builder.font
    locator, data = newTable("EBLC"), newTable("EBDT")
    locator.version = data.version = 2.0
    locator.strikes, data.strikeData = [], []
    for ppem in sizes:
        strike = eblc.Strike()
        size = strike.bitmapSizeTable
        size.colorRef, size.ppemX, size.ppemY = 0, ppem, ppem
        size.bitDepth, size.flags = bit_depth, 1
        for name in ("hori", "vert"):
            line = eblc.SbitLineMetrics()
            for field in eblc.sbitLineMetricsFormat.splitlines()[2:]:
                setattr(line, field.split(":")[0].strip(), 0)
            line.ascender, line.descender, line.widthMax = ppem - 2, -2, 8
            setattr(size, name, line)
        subtable = locator.getIndexFormatClass(index_format)(None, None)
        subtable.indexFormat, subtable.imageFormat = index_format, image_format
        # Sparse IDs for formats that permit them; format 2 needs consecutive IDs.
        subtable.names = ["A", "N"] if index_format == 2 else ["A", "smile"]
        strike.indexSubTables = [subtable]
        strike_data = {}
        m = metrics(big=image_format in (5, 6, 7), width=bitmap.shape[1],
                    height=bitmap.shape[0], left=left, top=top)
        rows = []
        for row in bitmap:
            # Include low-intensity pixels in grayscale fixtures.
            samples = [1 if value else 0 for value in row]
            bits = [int(bit) for sample in samples for bit in f"{sample:0{bit_depth}b}"]
            rows.append(np.packbits(bits).tobytes())
        for name in subtable.names:
            image = data.getImageFormatClass(image_format)(None, None)
            if image_format != 5:
                image.metrics = m
            image.setRows(rows, bitDepth=bit_depth, metrics=m)
            strike_data[name] = image
        if index_format in (2, 5):
            subtable.metrics = metrics(width=bitmap.shape[1], height=bitmap.shape[0],
                                       left=left, top=top)
            subtable.imageSize = len(strike_data["A"].compile(font))
        locator.strikes.append(strike)
        data.strikeData.append(strike_data)
    font["EBLC"], font["EBDT"] = locator, data
    path = tmp_path / "strike.otb"
    font.save(path)
    return path


def ink_pixels(glyph):
    ys, xs = np.nonzero(glyph.bitmap)
    return set(zip(xs + glyph.left, glyph.top - ys - 1))


def check_api(font, chars):
    assert set(font.chars()) == set(chars)
    assert all(font.has(ch) for ch in chars)
    assert not font.has("missing")
    with pytest.raises(KeyError):
        font.glyph("missing")
    for ch in chars:
        glyph = font.glyph(ch)
        assert isinstance(glyph, BitmapGlyph)
        assert glyph.bitmap.dtype == np.bool_
        assert glyph.bitmap.ndim == 2


@pytest.mark.parametrize("suffix", [".bdf", ".bdf.gz", ".dat"])
def test_bdf(tmp_path, suffix):
    font = open_bitmap_font(write_bdf(tmp_path, suffix))
    check_api(font, "AN ")
    assert (font.ppem, font.ascent, font.descent) == (16, 14, 2)
    a = font.glyph("A")
    np.testing.assert_array_equal(a.bitmap, MASK)
    assert (a.left, a.top, a.advance) == (0, 14, 8)
    n = font.glyph("N")
    np.testing.assert_array_equal(n.bitmap, [[1, 0, 0, 0, 0, 0, 0, 0, 1],
                                           [0, 1, 1, 1, 1, 1, 1, 1, 0],
                                           [1, 1, 1, 1, 1, 1, 1, 1, 1]])
    assert (n.left, n.top, n.advance) == (-2, 2, 10)
    assert not font.glyph(" ").bitmap.any()
    assert font.glyph(" ").advance == 4


@pytest.mark.parametrize("remove, expected", [
    (["PIXEL_SIZE 16"], 16),
    (["PIXEL_SIZE 16", "SIZE 16 72 72"], 16),
])
def test_bdf_metric_fallbacks(tmp_path, remove, expected):
    text = bdf_text().replace("FONTBOUNDINGBOX 16 16", "FONTBOUNDINGBOX 16 18")
    for line in remove + ["FONT_ASCENT 14", "FONT_DESCENT 2"]:
        text = text.replace(line + "\n", "")
    font = open_bitmap_font(write_bdf(tmp_path, text=text))
    assert font.ppem == (18 if len(remove) == 2 else expected)
    assert (font.ascent, font.descent) == (16, 2)


@pytest.mark.parametrize("suffix", [".hex", ".hex.gz", ".dat", ".dat.gz"])
def test_hex(tmp_path, suffix):
    font = open_bitmap_font(write_hex(tmp_path, suffix))
    check_api(font, "A好 😀")
    assert (font.ppem, font.ascent, font.descent) == (16, 14, 2)
    np.testing.assert_array_equal(font.glyph("A").bitmap, MASK)
    np.testing.assert_array_equal(font.glyph("好").bitmap, WIDE)
    assert font.glyph("A").advance == 8
    assert font.glyph("好").advance == 16
    assert (font.glyph("好").left, font.glyph("好").top) == (0, 14)
    assert not font.glyph(" ").bitmap.any()


def test_invalid_hex_width(tmp_path):
    path = tmp_path / "invalid.hex"
    path.write_text("0041:FF\n")
    with pytest.raises(ValueError, match="32 or 64"):
        open_bitmap_font(path)


@pytest.mark.parametrize("suffix", [".otf", ".ttf", ".woff2"])
def test_outlines(tmp_path, suffix):
    path = write_outline(tmp_path, suffix)
    with pytest.raises(ValueError, match="ppem"):
        open_bitmap_font(path)
    font = open_bitmap_font(path, ppem=16)
    check_api(font, "ANW Z😀")
    assert (font.ppem, font.ascent, font.descent) == (16, 14, 2)
    a = font.glyph("A")
    assert (a.left, a.top, a.advance) == (1, 13, 8)
    assert a.bitmap.shape == (15, 5)
    np.testing.assert_array_equal(a.bitmap, MASK[1:, 1:6])
    n = font.glyph("N")
    assert (n.left, n.top, n.advance) == (-2, 2, 8)
    np.testing.assert_array_equal(n.bitmap, np.ones((3, 3), bool))
    assert not font.glyph(" ").bitmap.any()
    assert font.glyph(" ").advance == 8
    # Both same-direction contours contribute winding in their overlap.
    overlap = np.ones((4, 4), dtype=bool)
    overlap[0, 0] = overlap[-1, -1] = False
    np.testing.assert_array_equal(font.glyph("W").bitmap, overlap)
    assert ink_pixels(a) == ink_pixels(font.glyph("😀"))


def test_outline_os2_metrics_and_scaling(tmp_path):
    builder = outline_builder()
    del builder.font["hhea"]
    path = tmp_path / "no-hhea.otf"
    builder.save(path)
    font = open_bitmap_font(path, ppem=8)
    assert (font.ascent, font.descent) == (6, 2)
    assert font.glyph(" ").advance == 4


@pytest.mark.parametrize("image_format,index_format", [
    (1, 1), (1, 3), (1, 4), (2, 1), (2, 3), (5, 2), (5, 5), (6, 1), (7, 4),
])
def test_otb_formats(tmp_path, image_format, index_format):
    path = write_otb(tmp_path, image_format, index_format)
    font = open_bitmap_font(path)
    check_api(font, "AN" if index_format == 2 else "A😀")
    assert not font.has("Z")  # In the cmap, but absent from this strike.
    assert (font.ppem, font.ascent, font.descent) == (16, 14, 2)
    a = font.glyph("A")
    np.testing.assert_array_equal(a.bitmap, STRIKE_MASK)
    assert (a.left, a.top, a.advance) == (-2, 2, 8)
    # A sole strike is used even when the requested ppem does not match.
    assert open_bitmap_font(path, ppem=99).ppem == 16
    renamed = tmp_path / "strike.ttf"
    renamed.write_bytes(path.read_bytes())
    np.testing.assert_array_equal(open_bitmap_font(renamed).glyph("A").bitmap, STRIKE_MASK)


@pytest.mark.parametrize("image_format,index_format", [(1, 1), (2, 3), (5, 5), (6, 1), (7, 4)])
@pytest.mark.parametrize("bit_depth", [2, 4, 8])
def test_grayscale_strikes(tmp_path, image_format, index_format, bit_depth):
    font = open_bitmap_font(write_otb(tmp_path, image_format, index_format, bit_depth=bit_depth))
    np.testing.assert_array_equal(font.glyph("A").bitmap, STRIKE_MASK)


def test_strike_selection(tmp_path):
    path = write_otb(tmp_path, sizes=(12, 16))
    for ppem in (None, 13):
        with pytest.raises(ValueError, match="ppem"):
            open_bitmap_font(path, ppem=ppem)
    for ppem in (12, 16):
        font = open_bitmap_font(path, ppem=ppem)
        assert (font.ppem, font.ascent, font.descent) == (ppem, ppem - 2, 2)


def test_formats_agree(tmp_path):
    fonts = [open_bitmap_font(write_bdf(tmp_path)), open_bitmap_font(write_hex(tmp_path)),
             open_bitmap_font(write_outline(tmp_path), ppem=16)]
    for image_format, index_format in [(1, 1), (2, 3), (5, 5), (6, 1), (7, 4)]:
        fonts.append(open_bitmap_font(write_otb(tmp_path, image_format, index_format,
                                              bitmap=MASK, left=0, top=14)))
    assert all(ink_pixels(f.glyph("A")) == ink_pixels(fonts[0].glyph("A")) for f in fonts)


@pytest.mark.parametrize("relative", [
    "data/fonts/fusion/fusion-pixel-12px-proportional-zh_hans.bdf",
    "data/fonts/unifont/unifont-17.0.05.hex.gz",
])
def test_real_fonts(relative):
    path = Path(__file__).resolve().parents[1] / relative
    if not path.exists():
        pytest.skip(f"Missing optional font: {path}")
    font = open_bitmap_font(path)
    assert font.has("好")
    glyph = font.glyph("好")
    pixels = ink_pixels(glyph)
    assert pixels
    assert all(0 <= x < font.ppem and -font.descent <= y < font.ascent
               for x, y in pixels)
    xs, ys = zip(*pixels)
    assert max(xs) - min(xs) + 1 <= font.ppem
    assert max(ys) - min(ys) + 1 <= font.ppem
