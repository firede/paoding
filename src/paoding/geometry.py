"""Paths, flattening, rasters, and conversions between pathops and shapely.

Work happens in the em frame: 1000 units per em, y up, ideographic em box
0..1000 x -120..880. A Canvas maps that frame onto a square raster, y down.
"""

import re

import numpy as np
import pathops
import shapely
from fontTools.pens.basePen import BasePen
from PIL import Image, ImageDraw

EM_LEFT, EM_BOTTOM, EM_SIZE = 0.0, -120.0, 1000.0


class Canvas:
    def __init__(self, res=512, left=EM_LEFT, bottom=EM_BOTTOM, size=EM_SIZE):
        self.res = res
        self.left, self.top = left, bottom + size
        self.px = res / size  # pixels per unit

    @property
    def shape(self):
        return (self.res, self.res)

    def to_px(self, pts):
        """Font units -> raster coordinates (x right, y down), as float columns."""
        pts = np.asarray(pts, float)
        return np.column_stack([(pts[:, 0] - self.left) * self.px, (self.top - pts[:, 1]) * self.px])

    def pixel_centers(self, ys, xs):
        """Raster indices -> font units of the pixel centers."""
        return np.column_stack([self.left + (xs + 0.5) / self.px, self.top - (ys + 0.5) / self.px])

    def index(self, pts):
        """Font units -> (row, col) float indices for map_coordinates."""
        p = self.to_px(pts) - 0.5
        return p[:, 1], p[:, 0]

    def fill(self, contours, rule="evenodd"):
        """Fill flattened contours. evenodd for non-overlapping outlines; union to OR shapes."""
        if rule == "union":
            img = Image.new("L", self.shape, 0)
            draw = ImageDraw.Draw(img)
            for c in contours:
                draw.polygon([tuple(p) for p in self.to_px(c)], fill=255)
            return np.asarray(img) > 0
        out = np.zeros(self.shape, bool)
        for c in contours:
            img = Image.new("L", self.shape, 0)
            ImageDraw.Draw(img).polygon([tuple(p) for p in self.to_px(c)], fill=255)
            out ^= np.asarray(img) > 0
        return out

    def lines(self, polylines, width=1):
        img = Image.new("L", self.shape, 0)
        draw = ImageDraw.Draw(img)
        for pl in polylines:
            pts = [tuple(p) for p in self.to_px(pl)]
            if len(pts) > 1:
                draw.line(pts, fill=255, width=width)
            elif pts:
                draw.point(pts, fill=255)
        return np.asarray(img) > 0

    def path_mask(self, path):
        return self.fill(flatten(path))


class FlattenPen(BasePen):
    """Collects contours as point lists with curves flattened."""

    def __init__(self, steps=8, transform=None):
        super().__init__(None)
        self.steps = steps
        self.transform = transform
        self.contours = []
        self._cur = None

    def _add(self, p):
        self._cur.append(p)

    def _moveTo(self, p):
        self._cur = [p]
        self.contours.append(self._cur)

    def _lineTo(self, p):
        self._add(p)

    def _qCurveToOne(self, p1, p2):
        p0 = self._cur[-1]
        for t in np.linspace(0, 1, self.steps + 1)[1:]:
            a, b, c = (1 - t) ** 2, 2 * (1 - t) * t, t * t
            self._add((a * p0[0] + b * p1[0] + c * p2[0], a * p0[1] + b * p1[1] + c * p2[1]))

    def _curveToOne(self, p1, p2, p3):
        p0 = self._cur[-1]
        for t in np.linspace(0, 1, self.steps + 1)[1:]:
            a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3
            self._add((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                       a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))

    def _closePath(self):
        self._cur = None

    _endPath = _closePath


def flatten(drawable, steps=8):
    """Anything with draw(pen) -> list of (n, 2) arrays, degenerate contours dropped."""
    pen = FlattenPen(steps)
    drawable.draw(pen)
    return [np.array(c) for c in pen.contours if len(c) > 2]


_SVG_TOKEN = re.compile(r"[MLQCZ]|-?\d+(?:\.\d+)?")


def draw_svg(d, pen, transform):
    """Draw an absolute M/L/Q/C/Z SVG path onto a pen, mapping points with transform."""
    toks = _SVG_TOKEN.findall(d)
    i = 0

    def pts(n):
        nonlocal i
        out = [transform(float(toks[i + 2 * k]), float(toks[i + 2 * k + 1])) for k in range(n)]
        i += 2 * n
        return out

    while i < len(toks):
        cmd = toks[i]
        i += 1
        if cmd == "M":
            pen.moveTo(*pts(1))
        elif cmd == "L":
            pen.lineTo(*pts(1))
        elif cmd == "Q":
            pen.qCurveTo(*pts(2))
        elif cmd == "C":
            pen.curveTo(*pts(3))
        elif cmd == "Z":
            pen.closePath()
        else:
            raise ValueError(f"unsupported SVG command {cmd!r}")


def transform_path(path, fn):
    """Map every point of a pathops Path through fn(x, y) -> (x, y)."""
    from fontTools.pens.filterPen import FilterPen

    class _Map(FilterPen):
        def moveTo(self, p):
            self._outPen.moveTo(fn(*p))

        def lineTo(self, p):
            self._outPen.lineTo(fn(*p))

        def curveTo(self, *ps):
            self._outPen.curveTo(*(fn(*p) for p in ps))

        def qCurveTo(self, *ps):
            self._outPen.qCurveTo(*(fn(*p) if p is not None else None for p in ps))

    out = pathops.Path()
    path.draw(_Map(out.getPen()))
    return out


def single_contour(contour):
    p = pathops.Path()
    contour.draw(p.getPen())
    return p


def op(a, b, kind):
    return pathops.op(a, b, getattr(pathops.PathOp, kind))


def union_all(paths):
    out = pathops.Path()
    for p in paths:
        out = op(out, p, "UNION")
    return out


def area(path):
    return abs(path.area)


def to_shapely(path, steps=8):
    """pathops Path (non-overlapping, even-odd) -> shapely geometry."""
    geom = shapely.Polygon()
    for c in flatten(path, steps):
        geom = geom.symmetric_difference(shapely.make_valid(shapely.Polygon(c)))
    return geom


def from_shapely(geom):
    """shapely (Multi)Polygon -> pathops Path of straight segments."""
    path = pathops.Path()
    for poly in getattr(geom, "geoms", [geom]):
        if poly.is_empty or poly.geom_type != "Polygon":
            continue
        for ring in [poly.exterior, *poly.interiors]:
            pts = list(ring.coords)[:-1]
            if len(pts) < 3:
                continue
            path.moveTo(*pts[0])
            for p in pts[1:]:
                path.lineTo(*p)
            path.close()
    return path


def polygons(geom):
    """Flatten any shapely geometry into its list of Polygons."""
    if geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    return [p for g in getattr(geom, "geoms", []) for p in polygons(g)]


def resample(poly, step=6.0):
    """Polyline -> points spaced about `step` apart."""
    seg = np.linalg.norm(np.diff(poly, axis=0), axis=1)
    t = np.concatenate([[0], np.cumsum(seg)])
    if t[-1] == 0:
        return poly[:1].copy()
    s = np.linspace(0, t[-1], max(2, int(np.ceil(t[-1] / step)) + 1))
    return np.column_stack([np.interp(s, t, poly[:, 0]), np.interp(s, t, poly[:, 1])])


def svg_d(path, ndigits=1):
    """pathops Path -> compact SVG path data."""
    from fontTools.pens.svgPathPen import SVGPathPen

    pen = SVGPathPen(None, ntos=lambda v: f"{round(v, ndigits):g}")
    path.draw(pen)
    return pen.getCommands()
