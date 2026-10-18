"""Split an outline glyph into the parts of its IDS decomposition.

For each unit of the target glyph (an outer contour with its holes):

- If medians of only one part run through it, the whole unit goes to that part.
- Otherwise the unit is cut. Each stroke claims a band around its registered
  median, as wide as the target's stroke there. A part's region is the union
  of its bands; ink no band covers joins the region it shares the longest
  border with. Cut lines are the region borders inside the unit; they split it
  into faces.
- A face inside the regions of several parts is shared: the overlay view gives
  it to all of them, the flat view to one owner. The owner is the stroke that
  passes through the junction; at a crossing (both pass) the part higher in
  the stacking order (later in the IDS by default); at a corner (neither
  passes) the part whose stroke comes first.

The annotation keeps only the unit's contour indices, the cut lines and a
labelled point per face; `realize` rebuilds the pieces from those and the font.
"""

from dataclasses import dataclass, field

import numpy as np
import pathops
import shapely
import shapely.ops
from scipy import ndimage
from skimage.morphology import skeletonize
from scipy.spatial import cKDTree

from . import ids
from .geometry import Canvas, area, from_shapely, op, polygons, resample, to_shapely, union_all
from .register import Warp, register, register_parts, skeleton

DEFAULT_THRESHOLD = 0.85


@dataclass
class Face:
    point: tuple             # a point inside the face, em frame
    nodes: tuple             # node paths whose overlay includes the face
    flat: tuple              # node path owning the face in the flat view
    rule: str = ""           # for shared faces: through | crossing | corner | override


@dataclass
class UnitResult:
    contours: list
    method: str              # direct | cut | override
    node: tuple = None       # direct: the owning node
    cuts: list = field(default_factory=list)    # polylines (n, 2), em frame
    faces: list = field(default_factory=list)   # cut: Face per face
    runs: dict = field(default_factory=dict)    # node -> length of its medians inside, units
    path: object = None      # pathops Path of the unit, em frame


@dataclass
class Segmentation:
    char: str
    glyph: str
    level: int
    ids: str
    nodes: list              # node paths in stacking order, bottom first
    units: list
    metrics: dict = field(default_factory=dict)
    confidence: float = 0.0
    status: str = "low"
    issues: list = field(default_factory=list)
    overrides: list = field(default_factory=list)
    debug: dict = field(default_factory=dict)
    grid: object = None      # bitmap targets: PixelLabels instead of units

    def pieces(self, view="overlay"):
        """node path -> pathops Path in the em frame, for view 'overlay' or 'flat'."""
        if self.grid is not None:
            return {n: self.grid.path(k, view) for k, n in enumerate(self.nodes)}
        out = {n: [] for n in self.nodes}
        for u in self.units:
            if u.method != "cut":
                out[u.node].append(u.path)
                continue
            for face, piece in zip(u.faces, realize(u.path, u.cuts, u.faces)):
                for n in (face.nodes if view == "overlay" else (face.flat,)):
                    out[n].append(piece)
        return {n: union_all(ps) for n, ps in out.items()}


# ---------- geometry of a cut unit ----------

def band(line, width):
    return shapely.LineString(line).buffer(width / 2, cap_style="flat", join_style="round")


def passes_through(line, region, margin):
    """Does the median continue past the region on both sides?"""
    ls = shapely.LineString(line)
    pts = shapely.get_coordinates(region.convex_hull.exterior if region.geom_type != "Point" else region)
    t = ls.project(shapely.points(pts))
    return t.min() > margin and ls.length - t.max() > margin


def realize(unit_path, cuts, faces, extend=2.0, pad=3.0):
    """Rebuild the pieces of a cut unit, one per face, keeping the original curves.

    The flattened unit is split along the cut lines; each split face, grown a
    little and with the other faces removed, clips the exact unit outline.
    """
    cells = _cells(to_shapely(unit_path, steps=16), cuts, extend)
    pts = shapely.points(np.array([f.point for f in faces]))
    owner = []
    for c in cells:
        inside = np.flatnonzero(shapely.contains(c, pts))
        owner.append(int(inside[0]) if len(inside) else int(np.argmin(shapely.distance(c, pts))))
    pieces = [pathops.Path() for _ in faces]
    taken = shapely.Polygon()
    for i, c in enumerate(cells):
        others = shapely.unary_union([d for j, d in enumerate(cells) if j != i])
        # the grown margins of neighbouring cells meet beyond the flattened outline: first come, first served
        clip = c.buffer(pad, join_style="mitre").difference(others).difference(taken)
        taken = taken.union(clip)
        piece = op(unit_path, from_shapely(clip), "INTERSECTION")
        pieces[owner[i]] = op(pieces[owner[i]], piece, "UNION")
    return pieces


def _cells(shape, cuts, extend=2.0):
    """The flattened unit split along the cut lines, as realize() sees it."""
    lines = [_extend(np.asarray(c), extend) for c in cuts]
    # snap-rounded noding: a cut ending on the outline joins it even at a grazing angle
    noded = shapely.unary_union([shape.boundary, *[shapely.LineString(l) for l in lines]], grid_size=GRID)
    return [f for f in polygons(shapely.GeometryCollection(list(shapely.polygonize(shapely.get_parts(noded)).geoms)))
            if shape.contains(f.representative_point())]


def _extend(line, d):
    """Extend a polyline by d at both ends, along its direction over the last unit or more."""
    if len(line) < 2:
        return line
    return np.vstack([line[0] + d * _heading(line), line, line[-1] + d * _heading(line[::-1])])


def _heading(line, back=1.0):
    """Unit vector out of the polyline's first point, measured from the first point at least `back` away."""
    far = np.linalg.norm(line - line[0], axis=1) >= back
    ref = line[np.argmax(far)] if far.any() else line[-1]
    return _unit(line[0] - ref)


def _unit(v):
    n = np.linalg.norm(v)
    return v / n if n else v


# ---------- segmentation ----------

def segment(char, font, mmah, level=1, override=None, canvas=None, min_run=45.0,
            threshold=DEFAULT_THRESHOLD, refine=True):
    override = override or {}
    canvas = canvas or Canvas()
    ref = mmah.reference(char, override.get("structure"), depth=level)
    nodes, stroke_node = ref.nodes(level)
    nodes = _stack(nodes, override.get("stack"))
    stroke_node = [nodes.index(ref.paths[i][:level]) for i in range(len(ref.paths))]
    units = font.units(char)
    masks = [canvas.path_mask(u.path) for u in units]
    ink = np.any(masks, axis=0)

    seg = Segmentation(char, font.glyph_name(char), level, ref.ids, nodes, [], issues=list(ref.issues))
    seg.overrides = sorted(k for k in override if override[k])
    if not ink.any():
        seg.issues.append("empty glyph")
        return seg

    warp = register(ref.medians, ref.strokes, ink, canvas)
    stroke_warps = [warp] * len(ref.medians)
    if refine:
        stroke_warps = _refine(seg, font, ref, units, masks, ink, canvas, warp, level, override, min_run)
    seg.debug.update(ref=ref, ink=ink, canvas=canvas, stroke_node=stroke_node)
    _assign(seg, font, units, masks, stroke_warps, override, min_run)

    seg.metrics = score(seg)
    seg.confidence = seg.metrics["confidence"]
    seg.status = override.get("status") or ("auto" if seg.confidence >= threshold else "low")
    return seg


def _refine(seg, font, ref, units, masks, ink, canvas, warp, level, override, min_run):
    """Per-part warps, top-down: at each depth, split with the current warps, then
    register every part of that depth on its own, starting from its parent's warp."""
    stroke_warps = [warp] * len(ref.medians)
    skel, ys, xs = skeleton(ink, canvas)
    for depth in range(1, level + 1):
        parts = sorted({p[:depth] for p in ref.paths})
        if len(parts) <= 1:
            continue
        stroke_node = [parts.index(p[:depth]) for p in ref.paths]
        trial = Segmentation(seg.char, seg.glyph, depth, ref.ids, parts, [])
        trial.debug.update(ref=ref, ink=ink, canvas=canvas, stroke_node=stroke_node)
        _assign(trial, font, units, masks, stroke_warps, override, min_run)
        overlay = trial.pieces("overlay")
        # parts that end above this depth are done
        todo = [k for k, p in enumerate(parts) if len(p) == depth]
        groups = [[i for i, s in enumerate(stroke_node) if s == k] for k in todo]
        owned = [canvas.path_mask(overlay[parts[k]])[ys, xs] for k in todo]
        current = [stroke_warps[g[0]] for g in groups]
        starts = [Warp(warp.affine)] * len(groups) if depth == 1 else current
        for g, w in zip(groups, register_parts(ref.medians, groups, starts, skel, owned, fallbacks=current)):
            for i in g:
                stroke_warps[i] = w
    return stroke_warps


def _assign(seg, font, units, masks, stroke_warps, override, min_run, spill_share=0.5):
    """Draw the strokes through their warps, then give each unit to a part or cut it."""
    dbg, canvas, nodes = seg.debug, seg.debug["canvas"], seg.nodes
    ref, ink, stroke_node = dbg["ref"], dbg["ink"], dbg["stroke_node"]
    lines = [w(resample(m)) for w, m in zip(stroke_warps, ref.medians)]
    inside = ndimage.distance_transform_edt(ink) / canvas.px
    widths = []
    for m in lines:
        v = ndimage.map_coordinates(inside, canvas.index(m), order=1)
        v = v[v > 0]
        widths.append(float(np.clip(2 * np.percentile(v, 75), 30, 160)) if len(v) else 70.0)
    # nearest stroke for every pixel
    dist = np.array([ndimage.distance_transform_edt(~canvas.lines([m], width=2)) for m in lines])
    nearest = np.array(stroke_node)[np.argmin(dist, 0)]
    dbg.update(lines=lines, widths=widths, nearest=nearest, stroke_warps=stroke_warps)

    # length of every stroke's median inside every unit
    run = np.zeros((len(lines), len(units)))
    hits = []                           # per stroke and unit: which median points lie in it
    for i, m in enumerate(lines):
        r, c = np.round(canvas.index(m)).astype(int)
        ok = (r >= 0) & (r < canvas.res) & (c >= 0) & (c < canvas.res)
        r, c = np.where(ok, r, 0), np.where(ok, c, 0)
        hits.append([mask[r, c] & ok for mask in masks])
        run[i] = [h.sum() * _spacing(m) for h in hits[i]]
    # a stroke is one piece of ink, so it lies in one unit; where its median reaches
    # into another unit for only a stretch (a reference tail running longer, pulled
    # across a gap onto a neighbour's ink), that stretch is not the part's ink there
    spill = (run > 0) & (run < spill_share * run.max(axis=1, keepdims=True))
    dbg["spilt"] = [np.any([hits[i][j] for j in np.flatnonzero(spill[i])], axis=0) if spill[i].any()
                    else np.zeros(len(m), bool) for i, m in enumerate(lines)]

    forced = {int(k): ids.unfmt(v) for k, v in (override.get("units") or {}).items()}
    for j, (u, mask) in enumerate(zip(units, masks)):
        runs = np.zeros(len(nodes))
        np.add.at(runs, stroke_node, np.where(spill[:, j], 0.0, run[:, j]))
        res = UnitResult(list(u.contours), "direct", path=u.path,
                         runs={nodes[k]: round(float(v), 1) for k, v in enumerate(runs) if v > 0})
        hit = [c for c in u.contours if c in forced]
        present = np.flatnonzero(runs >= min_run)
        if hit:
            res.method, res.node = "override", forced[hit[0]]
        elif len(nodes) == 1 or len(present) <= 1:
            k = int(present[0]) if len(present) == 1 else int(np.bincount(nearest[mask], minlength=len(nodes)).argmax())
            res.node = nodes[k]
        else:
            res.method = "cut"
            flat = [(font.to_em(*o["point"]), ids.unfmt(o["node"])) for o in override.get("flat") or []]
            _cut(res, u, set(int(k) for k in present), seg, flat, skip=set(np.flatnonzero(spill[:, j])))
        seg.units.append(res)


def _spacing(line):
    return float(np.linalg.norm(np.diff(line, axis=0), axis=1).mean()) if len(line) > 1 else 0.0


def _stack(nodes, order):
    """Stacking order, bottom first: IDS order unless overridden."""
    if not order:
        return nodes
    wanted = [ids.unfmt(n) for n in order]
    return [n for n in wanted if n in nodes] + [n for n in nodes if n not in wanted]


def _cut(res, unit, present, seg, flat_overrides, skip=(), min_face=25.0):
    dbg = seg.debug
    ref, lines, widths, stroke_node = dbg["ref"], dbg["lines"], dbg["widths"], dbg["stroke_node"]
    nodes = seg.nodes
    shape = to_shapely(unit.path, steps=16)
    strokes = [i for i in range(len(lines)) if stroke_node[i] in present and i not in skip]
    bands = {i: _areal(band(lines[i], widths[i]).intersection(shape)) for i in strokes}
    region = {}
    for i in strokes:
        k = stroke_node[i]
        region[k] = bands[i] if k not in region else _areal(region[k].union(bands[i]))
    # ink outside every band joins the region it borders most
    covered = shapely.unary_union(list(region.values()))
    for blob in polygons(shape.difference(covered)):
        contact = {k: blob.boundary.intersection(g.buffer(0.5)).length for k, g in region.items()}
        k = max(contact, key=contact.get)
        if contact[k] == 0:
            r, c = np.round(dbg["canvas"].index(np.array(blob.representative_point().coords))).astype(int)[:, 0]
            k = int(dbg["nearest"][r, c])
        region[k] = _areal(region[k].union(blob))

    # faces: one arrangement of the outline and all region boundaries, snap-rounded
    # together so nearly coincident edges become one; every segment then knows
    # exactly which faces it separates
    outline = shape.boundary
    linework = shapely.unary_union([outline, *[g.boundary for g in region.values()]], grid_size=GRID)
    inside = shapely.set_precision(shape, GRID)
    faces = [f for f in polygons(shapely.GeometryCollection(list(shapely.polygonize(shapely.get_parts(linework)).geoms)))
             if f.area > 1e-3 and inside.contains(f.representative_point())]
    keys = sorted(region)
    labels = []
    for f in faces:
        lab = tuple(k for k in keys if region[k].intersection(f).area > 0.5 * f.area)
        if not lab:
            p = f.representative_point()
            r, c = np.round(dbg["canvas"].index(np.array([[p.x, p.y]]))).astype(int)
            lab = (int(dbg["nearest"][r[0], c[0]]),)
        labels.append(lab)
    sides = {}                          # segment -> the faces either side of it
    for i, f in enumerate(faces):
        for seg_key in _segments(f):
            sides.setdefault(seg_key, []).append(i)
    border = [{} for _ in faces]        # face -> {neighbour: shared border length}
    for seg_key, fs in sides.items():
        if len(fs) == 2:
            a, b = fs
            border[a][b] = border[b][a] = border[a].get(b, 0.0) + _seg_length(seg_key)
    # tiny faces join the neighbour they share the most border with
    for i, f in enumerate(faces):
        if f.area >= min_face:
            continue
        contact = [(n, j) for j, n in border[i].items() if faces[j].area >= min_face]
        if contact:
            labels[i] = labels[max(contact)[1]]

    out = []
    for f, lab in zip(faces, labels):
        p = f.representative_point()
        node_set = tuple(nodes[k] for k in lab)
        if len(lab) == 1:
            out.append(Face((p.x, p.y), node_set, node_set[0]))
            continue
        owner, rule = _owner(f, lab, strokes, lines, widths, stroke_node, nodes)
        for point, node in flat_overrides:
            if node in node_set and f.buffer(1.0).contains(shapely.Point(*point)):
                owner, rule = node, "override"
        out.append(Face((p.x, p.y), node_set, owner, rule))
    sig = [(face.nodes, face.flat) for face in out]

    # faces with the same labels that share an edge are one face; cut lines separate the rest
    group = list(range(len(faces)))
    def find(i):
        while group[i] != i:
            group[i] = group[group[i]]
            i = group[i]
        return i
    for a in range(len(faces)):
        for b in border[a]:
            if sig[a] == sig[b]:
                group[find(a)] = find(b)
    members = {}
    for i in range(len(faces)):
        members.setdefault(find(i), []).append(i)
    merged = [[_merge([faces[i] for i in m]), out[m[0]]] for m in members.values()]
    sep = [shapely.LineString(np.array(seg_key) * GRID) for seg_key, fs in sides.items()
           if len(fs) == 2 and sig[fs[0]] != sig[fs[1]]]
    merged_lines = shapely.get_parts(shapely.line_merge(shapely.unary_union(sep))) if sep else []
    res.cuts = [_to_outline(np.array(piece.simplify(0.5).coords), outline)
                for line in merged_lines for piece in _split_at_outline(line, inside.boundary)]
    dbg.setdefault("faces", []).append((inside, merged))
    res.faces = []
    for g, face in merged:
        p = g.representative_point()
        res.faces.append(Face((round(p.x, 1), round(p.y, 1)), face.nodes, face.flat, face.rule))
    # a face can fall apart into several cells when rebuilt (where the outline pinches,
    # snap-rounding joins its two sides): every cell needs a point, or it takes the nearest
    pts = shapely.points(np.array([f.point for f in res.faces]))
    # (hairline cells may take the nearest point; one rounded off into a neighbour would steal it)
    for cell in _cells(shape, res.cuts):
        if cell.area < min_face or shapely.contains(cell, pts).any():
            continue
        g, face = max(merged, key=lambda gf: gf[0].intersection(cell).area)
        p = cell.representative_point()
        p = (round(p.x, 1), round(p.y, 1))
        if cell.contains(shapely.Point(p)):
            res.faces.append(Face(p, face.nodes, face.flat, face.rule))


GRID = 0.5    # snap-rounding grid, em units (1/1000 em): far below anything visible


def _merge(polys):
    """Union of adjacent faces on the GRID; a hairline group that snapping would
    collapse to nothing is kept unsnapped."""
    g = shapely.unary_union(polys, grid_size=GRID)
    return g if not g.is_empty else shapely.unary_union(polys)


def _segments(poly):
    """Boundary segments of a polygon on the GRID lattice, as order-free integer keys."""
    for ring in (poly.exterior, *poly.interiors):
        c = np.round(np.asarray(ring.coords) / GRID).astype(int)
        for a, b in zip(map(tuple, c[:-1]), map(tuple, c[1:])):
            if a != b:
                yield min(a, b), max(a, b)


def _seg_length(seg_key):
    (x0, y0), (x1, y1) = seg_key
    return GRID * float(np.hypot(x1 - x0, y1 - y0))


def _split_at_outline(line, outline, touch=0.6):
    """Split a polyline at inner vertices touching the outline: each piece then
    runs outline to outline (or junction), never along a hairline gap."""
    pts = np.array(line.coords)
    near = shapely.distance(shapely.points(pts), outline) < touch
    pieces, start = [], 0
    for i in range(1, len(pts) - 1):
        if near[i]:
            pieces.append(shapely.LineString(pts[start:i + 1]))
            start = i
    pieces.append(shapely.LineString(pts[start:]))
    return [p for p in pieces if p.length > 0]


def _to_outline(cut, outline, reach=1.5):
    """Run cut ends lying just inside the outline onto it, so no hairline gap joins two faces."""
    ends = []
    for p in (cut[0], cut[-1]):
        q = shapely.ops.nearest_points(outline, shapely.Point(p))[0]
        d = q.distance(shapely.Point(p))
        ends.append(np.array([[q.x, q.y]]) if 0.01 < d < reach else np.empty((0, 2)))
    return np.vstack([ends[0], cut, ends[1]])


def _areal(geom):
    """Keep only the polygonal part of a shapely result (drop slivers of lines and points)."""
    return shapely.unary_union(polygons(geom))


def _owner(face, lab, strokes, lines, widths, stroke_node, nodes):
    involved = [i for i in strokes if stroke_node[i] in lab and band(lines[i], widths[i]).intersects(face)]
    through = sorted({stroke_node[i] for i in involved
                      if passes_through(lines[i], face, 0.3 * widths[i])})
    if len(through) == 1:
        return nodes[through[0]], "through"
    if len(through) > 1:
        return nodes[max(through)], "crossing"
    if not involved:
        return nodes[max(lab)], "corner"
    # neither passes: the part whose strokes run furthest inside wins, then the earlier stroke
    run = {}
    for i in involved:
        k = stroke_node[i]
        run[k] = run.get(k, 0.0) + shapely.LineString(lines[i]).intersection(face).length
    first = {k: min(i for i in involved if stroke_node[i] == k) for k in run}
    best = max(run, key=lambda k: (round(run[k] / 10), -first[k]))
    return nodes[best], "corner"


# ---------- scoring ----------

def _core(line, spilt=None):
    """The middle 70% of a median, without points spilt onto another unit (unless that is all of it)."""
    a, b = int(len(line) * 0.15), max(int(len(line) * 0.85), 1) + 1
    if spilt is None or spilt[a:b].all():
        return line[a:b]
    return line[a:b][~spilt[a:b]]


def score(seg, slack=0.0, node_purity=None):
    """How well the registered reference explains the split, per node.

    slack: distance a median may stray off its ink and still count as on it
    (bitmaps: half a pixel). node_purity: per-node values replacing the
    measured purity (bitmaps measure it on pixel runs).

    median_in: share of a node's median points (ends trimmed) on or near its overlay ink.
    purity: share of the skeleton of a node's flat ink not clearly nearer another node's medians.
    skel_cov: share of that skeleton near the node's own medians (style-limited; informative).
    iou: flat ink vs the warped reference strokes of the node (style-limited).
    """
    dbg = seg.debug
    canvas, ref, lines, widths = dbg["canvas"], dbg["ref"], dbg["lines"], dbg["widths"]
    stroke_node, stroke_warps = dbg["stroke_node"], dbg["stroke_warps"]
    spilt = dbg.get("spilt")
    overlay = {n: canvas.path_mask(p) for n, p in seg.pieces("overlay").items()}
    flat = {n: canvas.path_mask(p) for n, p in seg.pieces("flat").items()}
    dbg.update(overlay_masks=overlay, flat_masks=flat)
    ys, xs = np.nonzero(skeletonize(dbg["ink"]))
    skel = canvas.pixel_centers(ys, xs)
    per = {}
    for k, n in enumerate(seg.nodes):
        idx = [i for i, s in enumerate(stroke_node) if s == k]
        pts = np.concatenate([lines[i] for i in idx])
        width = float(np.median([widths[i] for i in idx]))
        # stroke ends differ most between styles (kaishu dots and tails run
        # longer): judge the middle 70% of each median, with some slack
        # stretches spilt onto another unit's ink are not the part's (see _assign)
        core = np.concatenate([_core(lines[i], spilt[i] if spilt else None) for i in idx])
        near = ndimage.distance_transform_edt(~overlay[n]) / canvas.px
        inside = ndimage.map_coordinates(near, canvas.index(core), order=1) <= max(0.25 * width, slack)
        sp = skel[flat[n][ys, xs]]
        if len(sp):
            own = cKDTree(pts).query(sp)[0]
            cov = float((own < 0.75 * width + 15).mean())
            # ink that sits clearly nearer another part's strokes than its own
            others = [lines[i] for i, s in enumerate(stroke_node) if s != k]
            other = cKDTree(np.concatenate(others)).query(sp)[0] if others else np.full(len(sp), np.inf)
            purity = float(1 - ((own > 0.75 * width + 15) & (other < own - 0.5 * width)).mean())
        else:
            cov = purity = 0.0
        if node_purity is not None:
            purity = node_purity[n]
        refmask = canvas.fill([stroke_warps[i](ref.strokes[i]) for i in idx], rule="union")
        iou = (flat[n] & refmask).sum() / max((flat[n] | refmask).sum(), 1)
        per[ids.fmt(n)] = {"median_in": round(float(inside.mean()), 3), "purity": round(purity, 3), "skel_cov": round(cov, 3),
                           "iou": round(float(iou), 3), "area": int(flat[n].sum())}
    empty = [k for k, v in per.items() if v["area"] == 0]
    conf = 0.0 if empty else min(min(v["median_in"], v["purity"]) for v in per.values())
    rules = [f.rule for u in seg.units for f in u.faces if f.rule]
    if any(r in ("crossing", "corner") for r in rules):
        conf *= 0.95
    if any(s not in ("matches", "whole", "override") for s in ref.sources):
        conf *= 0.95
    if any("has no strokes" in i for i in ref.issues):
        conf *= 0.5
    elif ref.issues:
        conf *= 0.8
    return {
        "nodes": per,
        "units": len(seg.units),
        "cut_units": sum(u.method == "cut" for u in seg.units),
        "junctions": {r: rules.count(r) for r in sorted(set(rules))},
        "empty": empty,
        "inferred_strokes": sum(s not in ("matches", "whole", "override") for s in ref.sources),
        "confidence": round(conf, 3),
    }
