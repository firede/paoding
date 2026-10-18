"""paoding inspect: rerun characters with their debug state laid out for diagnosis.

For each character, writes into <out>/<char>/:

- panels.png: registration (warped reference medians, numbered by stroke, over
  the target ink) | flat with cut lines | overlay, then one panel per node
  (its overlay ink and its own medians). A grid line every 100 font units;
  marked points are circled.
- inspect.json: parts with their scores, strokes (node, MMAH path, how the path
  was decided), units with their faces, what lies at each marked point, and
  whether the result still matches the run's record.

The panels draw reference geometry (from MMAH) and are for diagnosis only.
"""

import json
from pathlib import Path

import numpy as np
import shapely
from PIL import Image, ImageDraw

from . import ids, record
from .geometry import to_shapely
from .report import COLORS, _font
from .review import part_label
from .segment import realize

PANEL = 512


def parse_targets(tokens):
    """["照", "蔗@512,300", "蔗@80,90"] -> {"照": [], "蔗": [(512, 300), (80, 90)]}, in order."""
    out = {}
    for t in tokens:
        char, _, at = t.partition("@")
        if len(char) != 1:
            raise ValueError(f"not one character: {t!r}")
        pts = out.setdefault(char, [])
        if at:
            x, y = (float(v) for v in at.split(","))
            pts.append((x, y))
    return out


def run_records(run_dir):
    lines = (Path(run_dir) / "records.jsonl").read_text(encoding="utf-8").splitlines()
    return json.loads(lines[0]), {r["char"]: r for r in map(json.loads, lines[1:])}


def inspect_char(seg, font, points, out_dir, run_record=None):
    """Write panels.png and inspect.json for one segmentation; returns the summary dict."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rec = record.to_record(seg, font)
    em_points = [font.to_em(*p) for p in points]
    info = {
        "char": seg.char, "codepoint": rec["codepoint"], "glyph": seg.glyph, "level": seg.level, "ids": seg.ids,
        "status": seg.status, "confidence": seg.confidence, "issues": seg.issues,
        "overrides": seg.overrides, "panels": "panels.png",
    }
    if "canvas" not in seg.debug:
        info["record"] = rec
        (out_dir / "inspect.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
        return info
    dbg = seg.debug
    ref, lines = dbg["ref"], dbg["lines"]
    to_font = lambda p: [round(v, 1) for v in font.from_em(*p)]
    per = seg.metrics.get("nodes", {})
    info["notes"] = ref.notes
    info["nodes"] = [{"node": ids.fmt(n), "part": part_label(rec, ids.fmt(n)), **per.get(ids.fmt(n), {})}
                     for n in seg.nodes]
    info["junctions"] = seg.metrics.get("junctions", {})
    info["strokes"] = [{"stroke": i, "node": ids.fmt(seg.nodes[dbg["stroke_node"][i]]), "path": ids.fmt(ref.paths[i]),
                        "source": ref.sources[i], "width": round(dbg["widths"][i] / font.scale, 1)}
                       for i in range(len(lines))]
    info["units"] = []
    for k, u in enumerate(seg.units):
        entry = {"unit": k, "contours": u.contours, "method": u.method,
                 "runs": {ids.fmt(n): round(v / font.scale, 1) for n, v in u.runs.items()}}
        if u.method == "cut":
            entry["cuts"] = len(u.cuts)
            entry["faces"] = [{"at": to_font(f.point), "nodes": [ids.fmt(n) for n in f.nodes], "flat": ids.fmt(f.flat),
                               **({"rule": f.rule} if f.rule else {})} for f in u.faces]
        else:
            entry["node"] = ids.fmt(u.node)
        info["units"].append(entry)
    info["at"] = [_at(seg, font, p, q) for p, q in zip(points, em_points)]
    if run_record is not None:
        differs = sorted(k for k in set(rec) | set(run_record) if rec.get(k) != run_record.get(k))
        info["run"] = {"matches": not differs, "differs": differs}
    info["record"] = rec
    panels(seg, font, em_points).save(out_dir / "panels.png")
    (out_dir / "inspect.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    return info


def _at(seg, font, point, em, reach=40.0):
    """What a marked point falls on (or is nearest to, within reach em units): unit, face, nearest strokes.

    A point clicked by eye may land just off the ink; `distance` (font units) says how far.
    """
    p = shapely.Point(em)
    out = {"point": list(point), "unit": None, "face": None}
    d = [to_shapely(u.path).distance(p) for u in seg.units]
    if d and min(d) <= reach:
        k = int(np.argmin(d))
        u = seg.units[k]
        out["unit"], out["distance"] = k, round(d[k] / font.scale, 1)
        if u.method == "cut":
            pieces = realize(u.path, u.cuts, u.faces)
            f = u.faces[int(np.argmin([to_shapely(piece).distance(p) for piece in pieces]))]
            out["face"] = {"at": [round(v, 1) for v in font.from_em(*f.point)],
                           "nodes": [ids.fmt(n) for n in f.nodes], "flat": ids.fmt(f.flat),
                           **({"rule": f.rule} if f.rule else {})}
        else:
            out["owner"] = ids.fmt(u.node)
    dbg = seg.debug
    d = [float(shapely.LineString(m).distance(p)) if len(m) > 1 else float(np.linalg.norm(m[0] - em))
         for m in dbg["lines"]]
    out["nearest_strokes"] = [{"stroke": int(i), "node": ids.fmt(seg.nodes[dbg["stroke_node"][i]]),
                               "distance": round(d[i] / font.scale, 1)} for i in np.argsort(d)[:3]]
    return out


def panels(seg, font, em_points=()):
    dbg = seg.debug
    canvas, ink, lines, stroke_node = dbg["canvas"], dbg["ink"], dbg["lines"], dbg["stroke_node"]
    res = canvas.res
    grey = np.array((205, 205, 205))

    def base():
        img = np.full((res, res, 3), 255, np.uint8)
        img[ink] = grey
        return img

    def finish(arr, medians=(), label=None):
        im = Image.fromarray(arr.astype(np.uint8))
        d = ImageDraw.Draw(im)
        _grid(d, canvas, font)
        for i, c in medians:
            d.line([tuple(p) for p in canvas.to_px(lines[i])], fill=c, width=4)
            x, y = canvas.to_px(lines[i][:1])[0]
            d.text((x + 4, y - 16), str(i), fill=(0, 0, 0), font=_font(14))
        for q in em_points:
            x, y = canvas.to_px(np.array([q]))[0]
            d.ellipse((x - 12, y - 12, x + 12, y + 12), outline=(224, 57, 155), width=3)
        if label:
            d.text((6, 4), label, fill=(0, 0, 0), font=_font(16))
        return im

    color = lambda k: COLORS[k % len(COLORS)]
    reg = finish(base(), [(i, color(stroke_node[i])) for i in range(len(lines))], "配准")
    flat = np.full((res, res, 3), 255, np.uint8)
    for k, n in enumerate(seg.nodes):
        flat[dbg["flat_masks"][n]] = color(k)
    flat_im = finish(flat, label="拍平 + 切线")
    d = ImageDraw.Draw(flat_im)
    for u in seg.units:
        for line in u.cuts:
            d.line([tuple(p) for p in canvas.to_px(line)], fill=(0, 0, 0), width=2)
    over = np.full((res, res, 3), 255.0)
    for k, n in enumerate(seg.nodes):
        m = dbg["overlay_masks"][n]
        over[m] = over[m] * 0.45 + np.array(color(k)) * 0.55
    top = [reg, flat_im, finish(over, label="叠加")]
    per = seg.metrics.get("nodes", {})
    bottom = []
    for k, n in enumerate(seg.nodes):
        arr = base()
        arr[dbg["overlay_masks"][n]] = color(k)
        s = per.get(ids.fmt(n), {})
        label = f"{ids.fmt(n)} {part_label({'ids': seg.ids}, ids.fmt(n))}  in {s.get('median_in', 0):.2f} purity {s.get('purity', 0):.2f}"
        bottom.append(finish(arr, [(i, (0, 0, 0)) for i, s_ in enumerate(stroke_node) if s_ == k], label))
    cols = max(3, len(bottom))
    small = PANEL * 3 // cols
    out = Image.new("RGB", (PANEL * 3 + 8, PANEL + 4 + small), "white")
    for i, im in enumerate(top):
        out.paste(im.resize((PANEL, PANEL), Image.LANCZOS), (i * (PANEL + 4), 0))
    for i, im in enumerate(bottom):
        out.paste(im.resize((small, small), Image.LANCZOS), (i * (small + 2), PANEL + 4))
    return out


def _grid(draw, canvas, font, step=100):
    """A faint line every `step` font units, labelled along the left and bottom edges."""
    size = canvas.res / canvas.px
    (x0, y0), (x1, y1) = font.from_em(canvas.left, canvas.top - size), font.from_em(canvas.left + size, canvas.top)
    for v in np.arange(np.ceil(x0 / step) * step, x1, step):
        px = canvas.to_px(np.array([font.to_em(v, y0)]))[0][0]
        draw.line([(px, 0), (px, canvas.res)], fill=(232, 232, 236), width=1)
        draw.text((px + 2, canvas.res - 14), f"{v:g}", fill=(150, 150, 150), font=_font(10))
    for v in np.arange(np.ceil(y0 / step) * step, y1, step):
        py = canvas.to_px(np.array([font.to_em(x0, v)]))[0][1]
        draw.line([(0, py), (canvas.res, py)], fill=(232, 232, 236), width=1)
        draw.text((2, py + 1), f"{v:g}", fill=(150, 150, 150), font=_font(10))
