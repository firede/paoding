"""Outline (OTF/TTF) target fonts.

A glyph is read in its own units and mapped into the em frame (see geometry).
Its ink is grouped into units: connected pieces of the filled area, each an
outer boundary with its holes. A unit lists the original contours that make
it up, so annotations can refer to the font's own contour indices; this also
works for fonts whose contours overlap.
"""

from dataclasses import dataclass

import numpy as np
import pathops
from fontTools.ttLib import TTFont

import shapely

from ..geometry import area, flatten, op, single_contour, transform_path


@dataclass
class Unit:
    contours: list           # indices of the glyph's original contours
    path: object             # pathops Path, em frame, overlaps removed


class OutlineFont:
    kind = "outline"

    def __init__(self, path, font_number=0):
        self.path = str(path)
        self.font = TTFont(path, fontNumber=font_number, lazy=True)
        self.glyphs = self.font.getGlyphSet()
        self.cmap = self.font.getBestCmap()
        upm = self.font["head"].unitsPerEm
        self.scale = 1000 / upm
        os2 = self.font["OS/2"] if "OS/2" in self.font else None
        if os2 is not None and os2.sTypoAscender - os2.sTypoDescender == upm:
            descender = os2.sTypoDescender
        else:
            descender = -0.12 * upm
        self.shift = -120 - descender * self.scale

    @property
    def units_per_em(self):
        return self.font["head"].unitsPerEm

    def has(self, char):
        return ord(char) in self.cmap

    def glyph_name(self, char):
        return self.cmap[ord(char)]

    def to_em(self, x, y):
        return x * self.scale, y * self.scale + self.shift

    def from_em(self, x, y):
        return x / self.scale, (y - self.shift) / self.scale

    def contours(self, char):
        """The glyph's original contours, each as a pathops Path in the em frame."""
        raw = pathops.Path()
        self.glyphs[self.glyph_name(char)].draw(raw.getPen())
        return [transform_path(single_contour(c), self.to_em) for c in raw.contours]

    def units(self, char):
        originals = self.contours(char)
        full = pathops.Path()
        for c in originals:
            c.draw(full.getPen())
        merged = pathops.simplify(full, fix_winding=True)
        units = [Unit([], p) for p in _group(merged)]
        # an original contour belongs to the unit whose boundary it runs along;
        # where contours overlap, the parts left on the boundary still decide
        bounds = [shapely.MultiLineString([np.vstack([c, c[:1]]) for c in flatten(u.path)]) for u in units]
        for i, c in enumerate(originals):
            pts = shapely.points(np.concatenate(flatten(c)))
            d = [np.median(shapely.distance(pts, b)) for b in bounds]
            units[int(np.argmin(d))].contours.append(i)
        return [u for u in units if u.contours]


def _group(path):
    """Overlap-free path -> one Path per outer contour with the holes inside it."""
    contours = list(path.contours)
    outers = [i for i, c in enumerate(contours) if not c.clockwise]
    holes = [i for i, c in enumerate(contours) if c.clockwise]
    singles = [single_contour(c) for c in contours]
    groups = {o: [o] for o in outers}
    for h in holes:
        best = min((area(singles[o]), o) for o in outers
                   if area(op(singles[h], singles[o], "INTERSECTION")) > 0.9 * area(singles[h]))
        groups[best[1]].append(h)
    out = []
    for members in groups.values():
        p = pathops.Path()
        for i in members:
            contours[i].draw(p.getPen())
        out.append(p)
    return out
