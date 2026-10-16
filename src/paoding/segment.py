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
from scipy import ndimage
from skimage.morphology import skeletonize
from scipy.spatial import cKDTree

from . import ids
from .geometry import Canvas, area, from_shapely, op, polygons, resample, to_shapely, union_all
from .register import register

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
    shape = to_shapely(unit_path, steps=16)
    lines = [_extend(np.asarray(c), extend) for c in cuts]
    noded = shapely.unary_union([shape.boundary, *[shapely.LineString(l) for l in lines]])
    cells = [f for f in polygons(shapely.GeometryCollection(list(shapely.polygonize(shapely.get_parts(noded)).geoms)))
             if shape.contains(f.representative_point())]
    pts = shapely.points(np.array([f.point for f in faces]))
    owner = []
    for c in cells:
        inside = np.flatnonzero(shapely.contains(c, pts))
        owner.append(int(inside[0]) if len(inside) else int(np.argmin(shapely.distance(c, pts))))
    pieces = [pathops.Path() for _ in faces]
    for i, c in enumerate(cells):
        others = shapely.unary_union([d for j, d in enumerate(cells) if j != i])
        clip = c.buffer(pad, join_style="mitre").difference(others)
        piece = op(unit_path, from_shapely(clip), "INTERSECTION")
        pieces[owner[i]] = op(pieces[owner[i]], piece, "UNION")
    return pieces


def _extend(line, d):
    if len(line) < 2:
        return line
    a = line[0] + d * _unit(line[0] - line[1])
    b = line[-1] + d * _unit(line[-1] - line[-2])
    return np.vstack([a, line, b])


def _unit(v):
    n = np.linalg.norm(v)
    return v / n if n else v


# ---------- segmentation ----------

def segment(char, font, mmah, level=1, override=None, canvas=None, min_run=45.0,
            threshold=DEFAULT_THRESHOLD):
    override = override or {}
    canvas = canvas or Canvas()
    ref = mmah.reference(char, override.get("structure"))
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
    lines = [warp(resample(m)) for m in ref.medians]
    inside = ndimage.distance_transform_edt(ink) / canvas.px
    widths = []
    for m in lines:
        v = ndimage.map_coordinates(inside, canvas.index(m), order=1)
        v = v[v > 0]
        widths.append(float(np.clip(2 * np.percentile(v, 75), 30, 160)) if len(v) else 70.0)
    # nearest stroke for every pixel
    dist = np.array([ndimage.distance_transform_edt(~canvas.lines([m], width=2)) for m in lines])
    nearest = np.array(stroke_node)[np.argmin(dist, 0)]
    seg.debug.update(ref=ref, lines=lines, widths=widths, nearest=nearest, ink=ink, canvas=canvas,
                     stroke_node=stroke_node, warp=warp)

    forced = {int(k): ids.unfmt(v) for k, v in (override.get("units") or {}).items()}
    for u, mask in zip(units, masks):
        runs = np.zeros(len(nodes))
        for i, m in enumerate(lines):
            r, c = np.round(canvas.index(m)).astype(int)
            ok = (r >= 0) & (r < canvas.res) & (c >= 0) & (c < canvas.res)
            runs[stroke_node[i]] += mask[r[ok], c[ok]].sum() * _spacing(m)
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
            _cut(res, u, set(int(k) for k in present), seg, flat)
        seg.units.append(res)

    seg.metrics = score(seg)
    seg.confidence = seg.metrics["confidence"]
    seg.status = override.get("status") or ("auto" if seg.confidence >= threshold else "low")
    return seg


def _spacing(line):
    return float(np.linalg.norm(np.diff(line, axis=0), axis=1).mean()) if len(line) > 1 else 0.0


def _stack(nodes, order):
    """Stacking order, bottom first: IDS order unless overridden."""
    if not order:
        return nodes
    wanted = [ids.unfmt(n) for n in order]
    return [n for n in wanted if n in nodes] + [n for n in nodes if n not in wanted]


def _cut(res, unit, present, seg, flat_overrides, min_face=25.0):
    dbg = seg.debug
    ref, lines, widths, stroke_node = dbg["ref"], dbg["lines"], dbg["widths"], dbg["stroke_node"]
    nodes = seg.nodes
    shape = to_shapely(unit.path, steps=16)
    strokes = [i for i in range(len(lines)) if stroke_node[i] in present]
    bands = {i: band(lines[i], widths[i]).intersection(shape) for i in strokes}
    region = {}
    for i in strokes:
        k = stroke_node[i]
        region[k] = bands[i] if k not in region else region[k].union(bands[i])
    # ink outside every band joins the region it borders most
    covered = shapely.unary_union(list(region.values()))
    for blob in polygons(shape.difference(covered)):
        contact = {k: blob.boundary.intersection(g.buffer(0.5)).length for k, g in region.items()}
        k = max(contact, key=contact.get)
        if contact[k] == 0:
            r, c = np.round(dbg["canvas"].index(np.array(blob.representative_point().coords))).astype(int)[:, 0]
            k = int(dbg["nearest"][r, c])
        region[k] = region[k].union(blob)

    noded = shapely.unary_union([shape.boundary, *[g.boundary for g in region.values()]])
    faces = [f for f in polygons(shapely.GeometryCollection(list(shapely.polygonize(shapely.get_parts(noded)).geoms)))
             if shape.contains(f.representative_point()) and f.area > 1e-3]
    labels = []
    for f in faces:
        p = f.representative_point()
        lab = tuple(k for k in sorted(region) if region[k].buffer(1e-6).contains(p))
        if not lab:
            r, c = np.round(dbg["canvas"].index(np.array([[p.x, p.y]]))).astype(int)
            lab = (int(dbg["nearest"][r[0], c[0]]),)
        labels.append(lab)
    # tiny faces join the neighbour they share the most border with
    for i, f in enumerate(faces):
        if f.area >= min_face:
            continue
        contact = [(f.boundary.intersection(g.boundary).length, j) for j, g in enumerate(faces) if j != i and faces[j].area >= min_face]
        if contact:
            labels[i] = labels[max(contact)[1]]

    out_faces = []
    for f, lab in zip(faces, labels):
        p = f.representative_point()
        node_set = tuple(nodes[k] for k in lab)
        if len(lab) == 1:
            out_faces.append((f, Face((p.x, p.y), node_set, node_set[0])))
            continue
        owner, rule = _owner(f, lab, strokes, lines, widths, stroke_node, nodes)
        for point, node in flat_overrides:
            if node in node_set and f.buffer(1.0).contains(shapely.Point(*point)):
                owner, rule = node, "override"
        out_faces.append((f, Face((p.x, p.y), node_set, owner, rule)))

    # merge faces with identical labels that touch; cut lines separate the rest
    merged = []
    for f, face in out_faces:
        for m in merged:
            if (m[1].nodes, m[1].flat) == (face.nodes, face.flat) and m[0].buffer(1e-6).intersects(f):
                m[0] = m[0].union(f)
                break
        else:
            merged.append([f, face])
    cuts = []
    for a in range(len(merged)):
        for b in range(a + 1, len(merged)):
            shared = merged[a][0].boundary.intersection(merged[b][0].boundary)
            for line in shapely.get_parts(shapely.line_merge(shapely.unary_union(
                    [g for g in shapely.get_parts(shared) if g.geom_type in ("LineString", "MultiLineString")]))
                    if not shared.is_empty else shapely.GeometryCollection()):
                if line.length > 1:
                    cuts.append(np.array(line.simplify(0.5).coords))
    res.cuts = cuts
    res.faces = []
    for g, face in merged:
        p = g.representative_point()
        res.faces.append(Face((round(p.x, 1), round(p.y, 1)), face.nodes, face.flat, face.rule))


def _owner(face, lab, strokes, lines, widths, stroke_node, nodes):
    involved = [i for i in strokes if stroke_node[i] in lab and band(lines[i], widths[i]).intersects(face)]
    through = sorted({stroke_node[i] for i in involved
                      if passes_through(lines[i], face, 0.3 * widths[i])})
    if len(through) == 1:
        return nodes[through[0]], "through"
    if len(through) > 1:
        return nodes[max(through)], "crossing"
    first = min(involved, default=None)
    return (nodes[stroke_node[first]] if first is not None else nodes[max(lab)]), "corner"


# ---------- scoring ----------

def score(seg):
    """How well the registered reference explains the split, per node.

    median_in: share of a node's median points (ends trimmed) on or near its overlay ink.
    purity: share of the skeleton of a node's flat ink not clearly nearer another node's medians.
    skel_cov: share of that skeleton near the node's own medians (style-limited; informative).
    iou: flat ink vs the warped reference strokes of the node (style-limited).
    """
    dbg = seg.debug
    canvas, ref, lines, widths = dbg["canvas"], dbg["ref"], dbg["lines"], dbg["widths"]
    stroke_node, warp = dbg["stroke_node"], dbg["warp"]
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
        core = np.concatenate([lines[i][int(len(lines[i]) * 0.15):max(int(len(lines[i]) * 0.85), 1) + 1] for i in idx])
        near = ndimage.distance_transform_edt(~overlay[n]) / canvas.px
        inside = ndimage.map_coordinates(near, canvas.index(core), order=1) <= 0.25 * width
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
        refmask = canvas.fill([warp(ref.strokes[i]) for i in idx], rule="union")
        iou = (flat[n] & refmask).sum() / max((flat[n] | refmask).sum(), 1)
        per[ids.fmt(n)] = {"median_in": round(float(inside.mean()), 3), "purity": round(purity, 3), "skel_cov": round(cov, 3),
                           "iou": round(float(iou), 3), "area": int(flat[n].sum())}
    empty = [k for k, v in per.items() if v["area"] == 0]
    conf = 0.0 if empty else min(min(v["median_in"], v["purity"]) for v in per.values())
    rules = [f.rule for u in seg.units for f in u.faces if f.rule]
    if any(r in ("crossing", "corner") for r in rules):
        conf *= 0.95
    if any(s not in ("matches", "whole") for s in ref.sources):
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
