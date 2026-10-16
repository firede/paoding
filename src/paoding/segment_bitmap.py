"""Label each pixel of a bitmap glyph with the parts of its IDS decomposition.

Registration is the same as for outlines, on the glyph's pixels drawn as
squares. A pixel's flat owner is the part of the nearest registered stroke;
its overlay set adds every part whose stroke runs through the pixel, so at a
junction a pixel can belong to several parts. Isolated pixels whose owner
none of their neighbours share take their neighbours' owner.
"""

from dataclasses import dataclass

import numpy as np
import shapely
from scipy import ndimage
from skimage.morphology import skeletonize

from .geometry import Canvas, resample
from .register import register
from .segment import DEFAULT_THRESHOLD, Segmentation, _stack, score


@dataclass
class PixelLabels:
    glyph: object            # BitmapGlyph
    flat: np.ndarray         # (h, w) node index, -1 for no ink
    overlay: np.ndarray      # (n, h, w) bool
    font: object

    def path(self, k, view):
        mask = self.overlay[k] if view == "overlay" else self.flat == k
        return self.font.pixel_path(self.glyph, mask)


def segment_bitmap(char, font, mmah, level=1, override=None, canvas=None, threshold=DEFAULT_THRESHOLD):
    override = override or {}
    canvas = canvas or Canvas()
    ref = mmah.reference(char, override.get("structure"))
    nodes, _ = ref.nodes(level)
    nodes = _stack(nodes, override.get("stack"))
    stroke_node = [nodes.index(p[:level]) for p in ref.paths]
    g = font.bitmap(char)
    seg = Segmentation(char, font.glyph_name(char), level, ref.ids, nodes, [], issues=list(ref.issues))
    seg.overrides = sorted(k for k in override if override[k])
    ink = canvas.path_mask(font.pixel_path(g, g.bitmap))
    if not ink.any():
        seg.issues.append("empty glyph")
        return seg

    u = font.unit
    rows, cols = np.nonzero(g.bitmap)
    centers = np.column_stack([(g.left + cols + 0.5) * u, (g.top - rows - 0.5) * u])
    # thin on the pixel grid itself: 1px strokes stay as they are
    sr, sc = np.nonzero(skeletonize(g.bitmap))
    centerline = np.column_stack([(g.left + sc + 0.5) * u, (g.top - sr - 0.5) * u])
    warp = register(ref.medians, ref.strokes, ink, canvas, centerline=centerline)
    lines = [warp(resample(m)) for m in ref.medians]
    inside = ndimage.distance_transform_edt(ink) / canvas.px
    widths = []
    for m in lines:
        v = ndimage.map_coordinates(inside, canvas.index(m), order=1)
        v = v[v > 0]
        widths.append(float(2 * np.percentile(v, 75)) if len(v) else font.unit)

    pts = shapely.points(centers)
    dist = np.array([shapely.distance(pts, shapely.LineString(m) if len(m) > 1 else shapely.Point(m[0]))
                     for m in lines])
    nearest = np.array(stroke_node)[np.argmin(dist, 0)]

    h, w = g.bitmap.shape
    flat = np.full((h, w), -1)
    flat[rows, cols] = nearest
    flat = _despeckle(flat)
    overlay = np.zeros((len(nodes), h, w), bool)
    for i, d in enumerate(dist):
        through = d <= 0.5 * widths[i] + 0.2 * u
        overlay[stroke_node[i], rows[through], cols[through]] = True
    for k in range(len(nodes)):
        overlay[k] |= flat == k

    seg.grid = PixelLabels(g, flat, overlay, font)
    seg.debug.update(ref=ref, lines=lines, widths=widths, ink=ink, canvas=canvas,
                     stroke_node=stroke_node, warp=warp)
    seg.metrics = score(seg)
    seg.confidence = seg.metrics["confidence"]
    seg.status = override.get("status") or ("auto" if seg.confidence >= threshold else "low")
    return seg


def _despeckle(flat):
    """Give a pixel its neighbours' owner when no 8-neighbour shares its own."""
    out = flat.copy()
    h, w = flat.shape
    for r, c in zip(*np.nonzero(flat >= 0)):
        nb = flat[max(r - 1, 0):r + 2, max(c - 1, 0):c + 2].ravel()
        nb = nb[nb >= 0]
        same = (nb == flat[r, c]).sum() - 1
        if same == 0 and len(nb) > 1:
            others = nb[nb != flat[r, c]]
            out[r, c] = np.bincount(others).argmax()
    return out


def pixel_record(seg, font):
    """Rows of legend letters; each letter names an overlay set and a flat owner."""
    from . import ids
    grid = seg.grid
    legend, rows = {}, []
    letters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    for r in range(grid.flat.shape[0]):
        row = []
        for c in range(grid.flat.shape[1]):
            if grid.flat[r, c] < 0:
                row.append(".")
                continue
            key = (tuple(ids.fmt(seg.nodes[k]) for k in np.flatnonzero(grid.overlay[:, r, c])),
                   ids.fmt(seg.nodes[grid.flat[r, c]]))
            if key not in legend:
                legend[key] = letters[len(legend)]
            row.append(legend[key])
        rows.append("".join(row))
    return {
        "left": grid.glyph.left, "top": grid.glyph.top, "rows": rows,
        "legend": {v: {"nodes": list(k[0]), "flat": k[1]} for k, v in legend.items()},
    }
