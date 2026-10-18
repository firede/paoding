"""Run reports: contact sheets (PNG), summary and low list. The interactive page is `paoding review`."""

import json
from collections import Counter, defaultdict

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import ids

COLORS = [(222, 74, 54), (36, 120, 214), (46, 160, 84), (214, 150, 20), (142, 72, 196), (20, 160, 170)]
OP_NAMES = {"⿰": "lr", "⿱": "tb", "⿲": "lmr", "⿳": "tmb", "⿴": "full", "⿵": "open-b", "⿶": "open-t",
            "⿷": "open-r", "⿸": "ul", "⿹": "ur", "⿺": "ll", "⿻": "overlap"}
LABEL_FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"


def structure(ids_text):
    return OP_NAMES.get(ids_text[:1], "whole")


def _font(size):
    try:
        return ImageFont.truetype(LABEL_FONT, size)
    except OSError:
        return ImageFont.load_default()


# ---------- PNG ----------

def tile(seg, size=150):
    """Registration | flat | overlay, with a caption."""
    dbg = seg.debug
    if "canvas" not in dbg:
        img = Image.new("RGB", (size * 3 + 8, size + 34), "white")
        ImageDraw.Draw(img).text((4, size + 4), f"{seg.char} empty", fill=(200, 0, 0), font=_font(14))
        return img
    canvas, ink = dbg["canvas"], dbg["ink"]
    res = canvas.res
    panels = []

    reg = np.full((res, res, 3), 255, np.uint8)
    reg[ink] = (210, 210, 210)
    im = Image.fromarray(reg)
    d = ImageDraw.Draw(im)
    for i, line in enumerate(dbg["lines"]):
        c = COLORS[dbg["stroke_node"][i] % len(COLORS)]
        d.line([tuple(p) for p in canvas.to_px(line)], fill=c, width=5)
    panels.append(im)

    flat = np.full((res, res, 3), 255, np.uint8)
    for k, n in enumerate(seg.nodes):
        flat[dbg["flat_masks"][n]] = COLORS[k % len(COLORS)]
    im = Image.fromarray(flat)
    d = ImageDraw.Draw(im)
    for u in seg.units:
        for line in u.cuts:
            d.line([tuple(p) for p in canvas.to_px(line)], fill=(0, 0, 0), width=3)
    panels.append(im)

    over = np.full((res, res, 3), 255.0)
    for k, n in enumerate(seg.nodes):
        m = dbg["overlay_masks"][n]
        over[m] = over[m] * 0.45 + np.array(COLORS[k % len(COLORS)]) * 0.55
    panels.append(Image.fromarray(over.astype(np.uint8)))

    out = Image.new("RGB", (size * 3 + 8, size + 34), "white")
    for i, p in enumerate(panels):
        out.paste(p.resize((size, size), Image.LANCZOS), (i * (size + 4), 0))
    d = ImageDraw.Draw(out)
    ok = seg.status in ("auto", "verified")
    parts = " ".join(ids.leaves(seg.debug["ref"].tree) if not isinstance(seg.debug["ref"].tree, str) else seg.ids)
    d.text((2, size + 1), f"{seg.char} {structure(seg.ids)} {parts}", fill="black", font=_font(14))
    d.text((2, size + 17), f"{seg.status} {seg.confidence:.2f}  cut {seg.metrics.get('cut_units', 0)}/{len(seg.units)}",
           fill=(0, 120, 0) if ok else (200, 0, 0), font=_font(13))
    return out


def sheets(tiles, path_prefix, cols=4, rows=10):
    if not tiles:
        return []
    tw, th = tiles[0].size
    per = cols * rows
    paths = []
    for page in range(0, len(tiles), per):
        chunk = tiles[page:page + per]
        n_rows = -(-len(chunk) // cols)
        sheet = Image.new("RGB", (cols * (tw + 14), n_rows * (th + 10)), "white")
        for i, t in enumerate(chunk):
            sheet.paste(t, ((i % cols) * (tw + 14), (i // cols) * (th + 10)))
        p = f"{path_prefix}-{page // per + 1:02d}.png"
        sheet.save(p)
        paths.append(p)
    return paths


# ---------- text ----------

def summary(segs, records, threshold):
    by_struct = defaultdict(list)
    for s in segs:
        by_struct[structure(s.ids)].append(s)
    n = len(segs)
    auto = sum(s.status in ("auto", "verified") for s in segs)
    ious = [v["iou"] for s in segs for v in s.metrics.get("nodes", {}).values()]
    lines = [
        "# Run summary", "",
        f"- characters: {n}",
        f"- auto (confidence >= {threshold}): {auto} ({auto / max(n, 1):.1%})",
        f"- low: {sum(s.status == 'low' for s in segs)}",
        f"- units cut: {sum(s.metrics.get('cut_units', 0) for s in segs)} in "
        f"{sum(bool(s.metrics.get('cut_units')) for s in segs)} characters",
        f"- mean IoU with the warped reference: {np.mean(ious):.3f} (style-limited; read as a trend)" if ious else "",
        f"- junction rules: {dict(Counter(r for s in segs for r, c in s.metrics.get('junctions', {}).items() for _ in range(c)))}",
        "", "| structure | n | auto | mean conf | mean IoU |", "|---|---:|---:|---:|---:|",
    ]
    for k in sorted(by_struct):
        g = by_struct[k]
        gi = [v["iou"] for s in g for v in s.metrics.get("nodes", {}).values()]
        lines.append(f"| {k} | {len(g)} | {sum(s.status in ('auto', 'verified') for s in g)} | "
                     f"{np.mean([s.confidence for s in g]):.3f} | {np.mean(gi) if gi else 0:.3f} |")
    return "\n".join(l for l in lines if l is not None) + "\n"


def low_list(segs):
    rows = ["char\tids\tconfidence\tweakest\tcut_units\tjunctions\tissues"]
    for s in sorted(segs, key=lambda s: s.confidence):
        if s.status != "low":
            continue
        nodes = s.metrics.get("nodes", {})
        weakest = min(nodes.items(), key=lambda kv: min(kv[1]["median_in"], kv[1]["purity"]), default=(None, None))
        w = f"{weakest[0]}:in={weakest[1]['median_in']},purity={weakest[1]['purity']}" if weakest[0] is not None else ""
        rows.append("\t".join([s.char, s.ids, f"{s.confidence:.3f}", w, str(s.metrics.get("cut_units", 0)),
                               json.dumps(s.metrics.get("junctions", {})), "; ".join(s.issues)]))
    return "\n".join(rows) + "\n"
