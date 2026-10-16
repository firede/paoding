"""Annotation records (JSON Lines) and rebuilding geometry from them.

A record refers to the target font only through glyph names and contour
indices; cut lines and face points are in the font's own units. No reference
(MMAH) geometry is stored. See docs/format.md.
"""

import hashlib
from pathlib import Path

import numpy as np
import pathops

from . import __version__, ids
from .geometry import union_all
from .segment import Face, realize

FORMAT = 1


def _r(v):
    return round(float(v), 1)


def file_info(path):
    data = Path(path).read_bytes()
    return {"file": Path(path).name, "sha256": hashlib.sha256(data).hexdigest()}


def header(font, reference_files, params):
    return {
        "kind": "header",
        "format": FORMAT,
        "tool": f"paoding {__version__}",
        "font": file_info(font.path),
        "reference": [file_info(p) for p in reference_files],
        "params": params,
    }


def to_record(seg, font):
    pt = lambda p: [_r(v) for v in font.from_em(*p)]
    if seg.grid is not None:
        from .segment_bitmap import pixel_record
        body = {"pixels": pixel_record(seg, font)}
    else:
        body = {"units": _units(seg, pt)}
    return {
        "char": seg.char,
        "codepoint": f"U+{ord(seg.char):04X}",
        "glyph": seg.glyph,
        "level": seg.level,
        "ids": seg.ids,
        "nodes": [ids.fmt(n) for n in seg.nodes],
        "status": seg.status,
        "confidence": seg.confidence,
        **body,
        "metrics": seg.metrics,
        **({"issues": seg.issues} if seg.issues else {}),
        **({"overrides": seg.overrides} if seg.overrides else {}),
    }


def _units(seg, pt):
    units = []
    for u in seg.units:
        if u.method != "cut":
            rec = {"contours": u.contours, "node": ids.fmt(u.node)}
            if u.method == "override":
                rec["override"] = True
            units.append(rec)
            continue
        units.append({
            "contours": u.contours,
            "cuts": [[pt(p) for p in line] for line in u.cuts],
            "faces": [{"at": pt(f.point), "nodes": [ids.fmt(n) for n in f.nodes], "flat": ids.fmt(f.flat),
                       **({"rule": f.rule} if f.rule else {})} for f in u.faces],
        })
    return units


def glyph_contours(font, glyph_name):
    """The glyph's contours in font units, as pathops Paths."""
    raw = pathops.Path()
    font.glyphs[glyph_name].draw(raw.getPen())
    out = []
    for c in raw.contours:
        p = pathops.Path()
        c.draw(p.getPen())
        out.append(p)
    return out


def realize_record(rec, font):
    """Record + font -> {"overlay": {node: Path}, "flat": {node: Path}} in font units."""
    contours = glyph_contours(font, rec["glyph"])
    views = {"overlay": {n: [] for n in rec["nodes"]}, "flat": {n: [] for n in rec["nodes"]}}
    for u in rec["units"]:
        path = pathops.Path()
        for i in u["contours"]:
            contours[i].draw(path.getPen())
        path = pathops.simplify(path, fix_winding=True)
        if "node" in u:
            views["overlay"][u["node"]].append(path)
            views["flat"][u["node"]].append(path)
            continue
        faces = [Face(tuple(f["at"]), tuple(f["nodes"]), f["flat"]) for f in u["faces"]]
        for f, piece in zip(faces, realize(path, [np.array(c) for c in u["cuts"]], faces)):
            for n in f.nodes:
                views["overlay"][n].append(piece)
            views["flat"][f.flat].append(piece)
    return {v: {n: union_all(ps) for n, ps in d.items()} for v, d in views.items()}
