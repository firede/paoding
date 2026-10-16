"""Target fonts: outline (OTF/TTF) or bitmap (BDF, Unifont hex, EBDT strikes, pixel outlines)."""

from pathlib import Path

import numpy as np
import pathops

from ..geometry import EM_BOTTOM


class BitmapTarget:
    """A bitmap font seen through the same interface as OutlineFont.

    Pixel coordinates (x right, y up from the baseline) scale into the em
    frame by 1000 / ppem; the baseline stays at 0, so the ideographic em box
    is the pixel em shifted down by 12%.
    """

    kind = "bitmap"

    def __init__(self, path, ppem=None):
        from .bitmap import open_bitmap_font
        self.path = str(path)
        self.font = open_bitmap_font(path, ppem=ppem)
        self.ppem = self.font.ppem
        self.unit = 1000 / self.ppem

    @property
    def units_per_em(self):
        return self.ppem

    def has(self, char):
        g = self.font.has(char) and self.font.glyph(char)
        return bool(g) and g.bitmap.any()

    def glyph_name(self, char):
        return f"U+{ord(char):04X}"

    def bitmap(self, char):
        return self.font.glyph(char)

    def to_em(self, x, y):
        return x * self.unit, y * self.unit

    def from_em(self, x, y):
        return x / self.unit, y / self.unit

    def pixel_path(self, glyph, mask):
        """Union of the squares of the pixels in mask, em frame."""
        path = pathops.Path()
        u = self.unit
        for r, c in zip(*np.nonzero(mask)):
            x0, y1 = (glyph.left + c) * u, (glyph.top - r) * u
            path.moveTo(x0, y1)
            path.lineTo(x0, y1 - u)
            path.lineTo(x0 + u, y1 - u)
            path.lineTo(x0 + u, y1)
            path.close()
        return pathops.simplify(path, fix_winding=True)

    @property
    def em_bottom(self):
        return EM_BOTTOM


def open_target(path, ppem=None):
    suffix = "".join(Path(path).suffixes[-2:]).lower()
    bitmap_suffixes = (".bdf", ".hex", ".otb", ".bdf.gz", ".hex.gz")
    if suffix.endswith(bitmap_suffixes) or ppem is not None:
        return BitmapTarget(path, ppem)
    from .outline import OutlineFont
    return OutlineFont(path)
