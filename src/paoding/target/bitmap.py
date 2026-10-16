"""Read bitmap strikes and pixel outlines in baseline-relative coordinates."""

import gzip
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Protocol

import numpy as np
import pathops
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont


@dataclass(frozen=True)
class BitmapGlyph:
    bitmap: np.ndarray
    left: int
    top: int
    advance: int


class BitmapFont(Protocol):
    ppem: int
    ascent: int
    descent: int

    def has(self, ch: str) -> bool: ...
    def glyph(self, ch: str) -> BitmapGlyph: ...
    def chars(self) -> Iterator[str]: ...


class _Font:
    def __init__(self, glyphs, ppem, ascent, descent):
        self._glyphs = glyphs
        self.ppem, self.ascent, self.descent = ppem, ascent, descent

    def has(self, ch):
        return ch in self._glyphs

    def glyph(self, ch):
        return self._glyphs[ch]

    def chars(self):
        return iter(self._glyphs)


def _rows(rows, width):
    """Unpack byte-padded, MSB-first rows."""
    if not rows:
        return np.zeros((0, width), dtype=bool)
    data = np.array([list(row) for row in rows], dtype=np.uint8)
    return np.unpackbits(data, axis=1)[:, :width].astype(bool)


def _bdf(data):
    glyphs, props, current = {}, {}, None
    lines = iter(data.decode("latin-1").splitlines())
    for line in lines:
        fields = line.split()
        if not fields:
            continue
        key, values = fields[0], fields[1:]
        if key == "STARTCHAR":
            current = {}
        elif key == "ENDCHAR":
            cp = current.get("ENCODING", [-1])[0]
            if 0 <= cp <= 0x10FFFF:
                _, h, left, bottom = current["BBX"]
                glyphs[chr(cp)] = BitmapGlyph(
                    current["bitmap"], left, bottom + h, current["DWIDTH"][0]
                )
            current = None
        elif key == "BITMAP":
            width, height, *_ = current["BBX"]
            rows = [bytes.fromhex(next(lines).strip()) for _ in range(height)]
            current["bitmap"] = _rows(rows, width)
        elif current is not None and key in {"ENCODING", "BBX", "DWIDTH"}:
            current[key] = list(map(int, values))
        elif current is None and key in {
            "PIXEL_SIZE", "SIZE", "FONTBOUNDINGBOX", "FONT_ASCENT", "FONT_DESCENT"
        }:
            props[key] = list(map(int, values))
    box = props.get("FONTBOUNDINGBOX", [0, 0, 0, 0])
    ppem = props.get("PIXEL_SIZE", props.get("SIZE", [box[1]]))[0]
    ascent = props.get("FONT_ASCENT", [box[1] + box[3]])[0]
    descent = props.get("FONT_DESCENT", [-box[3]])[0]
    return _Font(glyphs, ppem, max(0, ascent), max(0, descent))


def _hex(data):
    glyphs = {}
    for line in data.decode("ascii").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        codepoint, bits = line.split(":")
        if len(bits) not in (32, 64):
            raise ValueError("Unifont hex glyphs must have 32 or 64 hex digits")
        width = len(bits) // 4
        raw = bytes.fromhex(bits)
        rows = [raw[i:i + width // 8] for i in range(0, len(raw), width // 8)]
        glyphs[chr(int(codepoint, 16))] = BitmapGlyph(_rows(rows, width), 0, 14, width)
    return _Font(glyphs, 16, 14, 2)


def _strike(font, ppem):
    strikes = font["EBLC"].strikes
    matches = [i for i, s in enumerate(strikes) if s.bitmapSizeTable.ppemY == ppem]
    if len(matches) == 1:
        index = matches[0]
    elif len(strikes) == 1:
        index = 0
    else:
        raise ValueError("Choose a ppem that identifies one bitmap strike")
    strike = strikes[index]
    size = strike.bitmapSizeTable
    if size.bitDepth not in (1, 2, 4, 8):
        raise ValueError(f"Unsupported bitmap bit depth: {size.bitDepth}")
    bitmaps = font["EBDT"].strikeData[index]
    by_name = {}
    for subtable in strike.indexSubTables:
        if subtable.imageFormat not in (1, 2, 5, 6, 7):
            raise ValueError(f"Unsupported EBDT image format: {subtable.imageFormat}")
        for name in subtable.names:
            image = bitmaps[name]
            metrics = subtable.metrics if subtable.imageFormat == 5 else image.metrics
            rows = [image.getRow(r, bitDepth=size.bitDepth, metrics=metrics)
                    for r in range(metrics.height)]
            bits = _rows(rows, metrics.width * size.bitDepth)
            bitmap = bits.reshape(metrics.height, metrics.width, size.bitDepth).any(axis=2)
            big = hasattr(metrics, "horiBearingX")
            by_name[name] = BitmapGlyph(
                bitmap,
                metrics.horiBearingX if big else metrics.BearingX,
                metrics.horiBearingY if big else metrics.BearingY,
                metrics.horiAdvance if big else metrics.Advance,
            )
    glyphs = {chr(cp): by_name[name] for cp, name in (font.getBestCmap() or {}).items()
              if name in by_name}
    return _Font(glyphs, size.ppemY, max(0, size.hori.ascender),
                 max(0, -size.hori.descender))


class _OutlineFont(_Font):
    def __init__(self, font, ppem):
        if ppem is None or ppem <= 0:
            raise ValueError("A positive ppem is required for pixel outlines")
        self._font = font
        self._scale = ppem / font["head"].unitsPerEm
        self._outlines = font.getGlyphSet()
        if "hhea" in font:
            ascent, descent = font["hhea"].ascent, font["hhea"].descent
        else:
            ascent, descent = font["OS/2"].sTypoAscender, font["OS/2"].sTypoDescender
        super().__init__({chr(cp): name for cp, name in (font.getBestCmap() or {}).items()},
                         ppem, max(0, round(ascent * self._scale)),
                         max(0, round(-descent * self._scale)))

    def glyph(self, ch):
        outline = self._outlines[self._glyphs[ch]]
        recording = DecomposingRecordingPen(self._outlines)
        outline.draw(recording)
        path = pathops.Path()
        path.fillType = pathops.FillType.WINDING
        scale = self._scale
        recording.replay(TransformPen(path.getPen(), (scale, 0, 0, scale, 0, 0)))
        advance = round(outline.width * scale)
        if not len(path):
            return BitmapGlyph(np.zeros((0, 0), dtype=bool), 0, 0, advance)
        xmin, ymin, xmax, ymax = path.bounds
        left, bottom = math.floor(xmin), math.floor(ymin)
        right, top = math.ceil(xmax), math.ceil(ymax)
        bitmap = np.array([[path.contains((x + 0.5, y - 0.5))
                            for x in range(left, right)]
                           for y in range(top, bottom, -1)], dtype=bool)
        return BitmapGlyph(bitmap.reshape(top - bottom, right - left), left, top, advance)


def open_bitmap_font(path, *, ppem: int | None = None) -> BitmapFont:
    """Open BDF, Unifont hex, EBDT strikes, or grid-aligned pixel outlines."""
    path = Path(path)
    data = path.read_bytes()
    if data.startswith(b"\x1f\x8b"):
        data = gzip.decompress(data)
    suffix = path.suffix.lower()
    if suffix == ".gz":
        suffix = path.with_suffix("").suffix.lower()
    if suffix == ".bdf" or data.lstrip().startswith(b"STARTFONT"):
        return _bdf(data)
    if suffix == ".hex" or re.match(rb"\s*(?:#[^\n]*\n\s*)*[0-9A-Fa-f]+:", data):
        return _hex(data)
    font = TTFont(BytesIO(data))
    if "EBLC" in font and "EBDT" in font:
        return _strike(font, ppem)
    return _OutlineFont(font, ppem)
