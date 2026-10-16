"""Label each pixel of a bitmap glyph with the parts of its IDS decomposition.

Registration is the same as for outlines (a global fit, then every part on
its own), on the glyph's pixels drawn as squares. A pixel's flat owner is the
part of the nearest registered stroke; its overlay set adds every part whose
stroke runs through the pixel, so at a junction a pixel can belong to several
parts. Isolated pixels whose owner none of their neighbours share take their
neighbours' owner, and a straight run of pixels, being one stroke, is mended
to one part where it can be (see _mend_runs).

Nearest-stroke labels make the outline purity meaningless (it is 1 by
construction), so a part's purity here is the share of its pixel runs that
no other part breaks into.
"""

from dataclasses import dataclass

import numpy as np
import shapely
from scipy import ndimage
from skimage.morphology import skeletonize

from .geometry import Canvas, resample
from .register import Warp, register, register_parts
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


def segment_bitmap(char, font, mmah, level=1, override=None, canvas=None, threshold=DEFAULT_THRESHOLD,
                   refine=True):
    override = override or {}
    canvas = canvas or Canvas()
    ref = mmah.reference(char, override.get("structure"), depth=level)
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

    px = _Pixels(g, font.unit, ink, canvas)
    # thin on the pixel grid itself: 1px strokes stay as they are
    sr, sc = np.nonzero(skeletonize(g.bitmap))
    centerline = px.center(sr, sc)
    warp = register(ref.medians, ref.strokes, ink, canvas, centerline=centerline)
    stroke_warps = [warp] * len(ref.medians)
    if refine:
        stroke_warps = _refine(ref, px, warp, level, centerline, (sr, sc))
    flat, overlay, lines, widths, broken = _label(ref, stroke_warps, stroke_node, len(nodes), px)

    seg.grid = PixelLabels(g, flat, overlay, font)
    seg.debug.update(ref=ref, lines=lines, widths=widths, ink=ink, canvas=canvas,
                     stroke_node=stroke_node, stroke_warps=stroke_warps)
    # nearest-stroke labels make the outline purity trivially 1: judge the
    # parts by the runs they break instead
    runs = np.zeros(len(nodes))
    for r, c in sum(_runs(flat >= 0), []):
        runs[np.bincount(flat[r, c]).argmax()] += 1
    hits = np.zeros(len(nodes))
    for k, j in broken:
        hits[[k, j]] += 1
    purity = {nodes[k]: 1 - hits[k] / max(runs[k], 2) for k in range(len(nodes))}
    seg.debug["broken_runs"] = broken
    seg.metrics = score(seg, slack=0.5 * font.unit, node_purity=purity)
    seg.confidence = seg.metrics["confidence"]
    seg.status = override.get("status") or ("auto" if seg.confidence >= threshold else "low")
    return seg


class _Pixels:
    """A bitmap glyph's ink pixels in the em frame."""

    def __init__(self, g, unit, ink, canvas):
        self.g, self.unit, self.shape = g, unit, g.bitmap.shape
        self.rows, self.cols = np.nonzero(g.bitmap)
        self.points = shapely.points(self.center(self.rows, self.cols))
        self.canvas = canvas
        self.inside = ndimage.distance_transform_edt(ink) / canvas.px

    def center(self, rows, cols):
        return np.column_stack([(self.g.left + cols + 0.5) * self.unit, (self.g.top - rows - 0.5) * self.unit])


def _refine(ref, px, warp, level, skel, skel_rc):
    """Per-part warps, top-down, as for outlines (segment._refine): label with the
    current warps, then register every part of a depth on the pixels it holds."""
    stroke_warps = [warp] * len(ref.medians)
    for depth in range(1, level + 1):
        parts = sorted({p[:depth] for p in ref.paths})
        if len(parts) <= 1:
            continue
        stroke_node = [parts.index(p[:depth]) for p in ref.paths]
        _, overlay, _, _, _ = _label(ref, stroke_warps, stroke_node, len(parts), px)
        todo = [k for k, p in enumerate(parts) if len(p) == depth]
        groups = [[i for i, s in enumerate(stroke_node) if s == k] for k in todo]
        owned = [overlay[k][skel_rc] for k in todo]
        current = [stroke_warps[g[0]] for g in groups]
        starts = [Warp(warp.affine)] * len(groups) if depth == 1 else current
        for g, w in zip(groups, register_parts(ref.medians, groups, starts, skel, owned,
                                               fallbacks=current, min_own=3)):
            for i in g:
                stroke_warps[i] = w
    return stroke_warps


def _label(ref, stroke_warps, stroke_node, n, px):
    """Flat owner and overlay sets of every ink pixel for the warped strokes."""
    canvas, u = px.canvas, px.unit
    lines = [w(resample(m)) for w, m in zip(stroke_warps, ref.medians)]
    widths = []
    for m in lines:
        v = ndimage.map_coordinates(px.inside, canvas.index(m), order=1)
        v = v[v > 0]
        widths.append(float(2 * np.percentile(v, 75)) if len(v) else u)
    dist = np.array([shapely.distance(px.points, shapely.LineString(m) if len(m) > 1 else shapely.Point(m[0]))
                     for m in lines])
    nearest = np.array(stroke_node)[np.argmin(dist, 0)]
    flat = np.full(px.shape, -1)
    flat[px.rows, px.cols] = nearest
    flat, broken = _mend_runs(_despeckle(flat))
    overlay = np.zeros((n, *px.shape), bool)
    for i, d in enumerate(dist):
        through = d <= 0.5 * widths[i] + 0.2 * u
        overlay[stroke_node[i], px.rows[through], px.cols[through]] = True
    for k in range(n):
        overlay[k] |= flat == k
    return flat, overlay, lines, widths, broken


def _runs(ink, min_len=3):
    """Maximal straight runs of ink pixels: (horizontal runs, vertical runs), each a
    list of (rows, cols) index arrays."""
    out = []
    for grid in (ink, ink.T):
        runs = []
        for r, line in enumerate(grid):
            edges = np.flatnonzero(np.diff(np.concatenate([[0], line.astype(int), [0]])))
            for a, b in zip(edges[::2], edges[1::2]):
                if b - a >= min_len:
                    runs.append((np.full(b - a, r), np.arange(a, b)))
        out.append(runs)
    horizontal, vertical = out
    return horizontal, [(c, r) for r, c in vertical]


def _mend_runs(flat):
    """Pixel strokes are straight runs, and a run is one stroke, so one part.

    A stretch inside a run given to another part, with the same part on both
    sides, goes back to that part; pixels where a perpendicular run crosses are
    left alone. Runs still split afterwards (another part at an end, or three
    parts in a row, over two or more pixels) are returned as
    (node of the run, node of the odd stretch) pairs: likely misassigned.
    """
    ink = flat >= 0
    horizontal, vertical = _runs(ink)
    out = flat.copy()
    broken = []
    for runs, across in ((horizontal, vertical), (vertical, horizontal)):
        crossing = np.zeros_like(ink)
        for r, c in across:
            crossing[r, c] = True
        for r, c in runs:
            # the run's own pixels, in order; crossings belong to the other stroke
            free = ~crossing[r, c]
            r, c = r[free], c[free]
            own = out[r, c]
            segs = np.split(np.arange(len(own)), np.flatnonzero(np.diff(own)) + 1)
            for i in range(1, len(segs) - 1):
                before, here, after = own[segs[i - 1][0]], own[segs[i][0]], own[segs[i + 1][0]]
                if before == after != here:
                    out[r[segs[i]], c[segs[i]]] = before
            own = out[r, c]
            if len(set(own)) > 1:
                major = np.bincount(own).argmax()
                odd = own[own != major]
                if len(odd) >= 2:
                    broken.append((int(major), int(np.bincount(odd).argmax())))
    return out, broken


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
