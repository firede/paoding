"""Review server: look through runs, select what looks wrong, copy it for an agent.

Glyphs are rebuilt from records.jsonl and the font with record.realize_record,
so the page shows exactly the geometry a consumer of the records gets. The
reviewer only looks and selects; the copied text names the run, the font and
the characters (optionally a part and a point in font units), with the
`paoding inspect` command that reproduces them.

A run is reread when its records.jsonl changes, so rerunning into the same
directory needs no restart. Selections can be kept as named groups, in their
own order, in a JSON file next to the runs; a group can be shown against two
runs side by side.

The page also shows the structure checks a run recorded (flags) and, on
request, the skeleton graph of a glyph: the joints and stretches those checks
are made on, computed from the font alone.
"""

import hashlib
import json
import os
import threading
import time
import unicodedata
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import numpy as np

from . import ids, skeleton
from .geometry import Canvas, svg_d
from .record import realize_record
from .report import COLORS, structure
from .testset import gb2312_level

PAGE = Path(__file__).with_name("review.html")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def is_hanzi(char):
    return unicodedata.name(char, "").startswith(("CJK UNIFIED IDEOGRAPH", "CJK COMPATIBILITY IDEOGRAPH"))


class Run:
    def __init__(self, path, font_sha):
        self.path, self.font_sha = str(path), font_sha
        self.file = Path(path) / "records.jsonl"
        self.version = None
        self.fresh()

    def fresh(self):
        """Reread records.jsonl if it changed since it was read; returns self."""
        version = self.file.stat().st_mtime_ns
        if version == self.version:
            return self
        lines = self.file.read_text(encoding="utf-8").splitlines()
        header = json.loads(lines[0])
        if header.get("kind") != "header":
            raise ValueError(f"{self.path}: records.jsonl has no header")
        if header["font"]["sha256"] != self.font_sha:
            raise ValueError(f"{self.path}: made from {header['font']['file']} with another SHA-256 than --font")
        records = {}
        for line in lines[1:]:
            rec = json.loads(line)
            if "units" not in rec:
                raise ValueError(f"{self.path}: review covers outline fonts only ({rec['char']} has no units)")
            records[rec["char"]] = rec
        self.header, self.records, self.version = header, records, version
        return self


def part_label(rec, node):
    """The IDS of the part a node path addresses, e.g. 昭 or ⿰日召."""
    try:
        return ids.unparse(ids.subtree(ids.parse(rec["ids"]), ids.unfmt(node)))
    except (ValueError, IndexError, TypeError):
        return "？"


def entry(rec):
    """What the grid needs to list, filter and sort one character."""
    per = rec["metrics"].get("nodes", {})
    return {
        "c": rec["char"], "cp": rec["codepoint"], "ids": rec["ids"], "st": rec["status"],
        "cf": rec["confidence"], "s": structure(rec["ids"]), "g": gb2312_level(rec["char"]),
        "n": [[n, part_label(rec, n), min(per[n]["median_in"], per[n]["purity"]) if n in per else None]
              for n in rec["nodes"]],
        "j": rec["metrics"].get("junctions", {}), "i": rec.get("issues", []), "o": rec.get("overrides", []),
        "f": len(rec.get("flags", [])),
    }


class Reviewer:
    def __init__(self, font, font_path, run_paths, mmah=None, groups_path=None):
        self.font, self.font_path = font, str(font_path)
        sha = sha256(font_path)
        self.font_info = {"path": self.font_path, "file": Path(font_path).name, "sha256": sha}
        self.runs = [Run(p, sha) for p in run_paths]
        self.mmah = mmah
        self.groups_path = Path(groups_path) if groups_path else None
        self.lock = threading.Lock()   # fontTools glyph sets load lazily and are not thread-safe
        self._cached = lru_cache(maxsize=8192)(self._glyph)
        self.skeleton = lru_cache(maxsize=1024)(self._skeleton)

    def run(self, k):
        return self.runs[k].fresh()

    def runs_info(self):
        return [{"path": r.path, "version": str(r.version), "level": r.header["params"].get("level"),
                 "overrides": r.header["params"].get("overrides", []), "count": len(r.records)}
                for r in (run.fresh() for run in self.runs)]

    def index(self, k):
        upm = self.font.units_per_em
        descender = self.font.from_em(0, -120)[1]
        run = self.run(k)
        return {"font": self.font_info, "run": self.runs_info()[k], "colors": ["#%02x%02x%02x" % c for c in COLORS],
                "viewbox": f"0 {-(upm + descender):g} {upm:g} {upm:g}", "entries": [entry(r) for r in run.records.values()]}

    def glyph(self, k, char):
        return self._cached(k, self.run(k).version, char)

    def _glyph(self, k, version, char):
        rec = self.runs[k].records[char]
        with self.lock:
            views = realize_record(rec, self.font)
        cuts = ["M" + " L".join(f"{x:g} {y:g}" for x, y in line) for u in rec["units"] for line in u.get("cuts", [])]
        return {v: {n: svg_d(p) for n, p in d.items()} for v, d in views.items()} | {"cuts": cuts, "flags": rec.get("flags", [])}

    def _skeleton(self, char):
        """The skeleton graph of a glyph in font units: stretches as SVG paths, joints as [x, y, r], bends and waists as [x, y]."""
        canvas = Canvas()
        with self.lock:
            if not self.font.has(char):
                raise KeyError(f"{char} is not in the font")
            units = self.font.units(char)
        ink = np.any([canvas.path_mask(u.path) for u in units], axis=0) if units else np.zeros(canvas.shape, bool)
        g = skeleton.graph(ink, canvas)
        pt = lambda p: [round(v, 1) for v in self.font.from_em(*p)]
        scale = abs(self.font.from_em(1, 0)[0] - self.font.from_em(0, 0)[0])
        return {
            "segments": ["M" + " L".join("%g %g" % tuple(pt(p)) for p in s.points[::2].tolist() + [s.points[-1].tolist()])
                         for s in g.segments],
            "joints": [pt(j.point) + [round(j.radius * scale, 1)] for j in g.joints],
            "bends": [pt(s.points[b]) for s in g.segments for b in s.bends + s.waists],
        }

    def gaps(self, k):
        """The font's hanzi without a record in this run, by why."""
        have = self.run(k).records
        missing = [chr(c) for c in sorted(self.font.cmap) if is_hanzi(chr(c)) and chr(c) not in have]
        if self.mmah is None:
            groups = [("not-run", "不在本次运行中", missing)]
        else:
            groups = [("no-mmah", "MMAH 没有此字，无结构数据", [c for c in missing if c not in self.mmah]),
                      ("not-run", "MMAH 有此字，但不在本次运行中（未选入、失败或被排除）",
                       [c for c in missing if c in self.mmah])]
        return {"hanzi": len(have) + len(missing), "covered": len(have),
                "groups": [{"key": key, "label": label, "chars": "".join(cs)} for key, label, cs in groups]}

    def groups(self):
        if self.groups_path is None or not self.groups_path.is_file():
            return []
        return json.loads(self.groups_path.read_text(encoding="utf-8"))["groups"]

    def save_groups(self, groups):
        """Replace the stored groups: [{id, name, created, items: [{run, char, node, at}]}], in order."""
        if self.groups_path is None:
            raise ValueError("no groups file")
        clean = []
        for g in groups:
            items = [{"run": str(i.get("run", "")), "char": str(i["char"]), "node": i.get("node"),
                      "at": [[float(x), float(y)] for x, y in i.get("at", [])]} for i in g["items"]]
            clean.append({"id": str(g["id"]), "name": str(g.get("name") or g["id"]),
                          "created": str(g.get("created") or time.strftime("%Y-%m-%dT%H:%M:%S")), "items": items})
        self.groups_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.groups_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"groups": clean}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.groups_path)
        return clean


def handler(reviewer):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, kind="application/json; charset=utf-8", status=200):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False,
                                                                   separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def run_index(self, q):
            k = int(q.get("run", ["0"])[0])
            if not 0 <= k < len(reviewer.runs):
                raise KeyError(f"no run {k}")
            return k

        def do_GET(self):
            url = urlsplit(self.path)
            q = parse_qs(url.query)
            try:
                if url.path == "/":
                    self.send(PAGE.read_bytes(), "text/html; charset=utf-8")
                elif url.path == "/api/runs":
                    self.send({"font": reviewer.font_info, "runs": reviewer.runs_info()})
                elif url.path == "/api/index":
                    self.send(reviewer.index(self.run_index(q)))
                elif url.path == "/api/gaps":
                    self.send(reviewer.gaps(self.run_index(q)))
                elif url.path == "/api/groups":
                    self.send({"groups": reviewer.groups(), "file": str(reviewer.groups_path or "")})
                elif url.path == "/api/skeleton":
                    self.send(reviewer.skeleton(q["char"][0]))
                else:
                    self.send({"error": "not found"}, status=404)
            except (KeyError, ValueError) as err:
                self.send({"error": str(err)}, status=400)

        def do_POST(self):
            url = urlsplit(self.path)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                if url.path == "/api/glyphs":
                    k = self.run_index({"run": [str(body.get("run", 0))]})
                    records = reviewer.run(k).records
                    self.send({c: reviewer.glyph(k, c) for c in body.get("chars", []) if c in records})
                elif url.path == "/api/groups":
                    self.send({"groups": reviewer.save_groups(body["groups"])})
                else:
                    self.send({"error": "not found"}, status=404)
            except (KeyError, ValueError, TypeError) as err:
                self.send({"error": str(err)}, status=400)

    return Handler


def serve(reviewer, host="127.0.0.1", port=8417):
    server = ThreadingHTTPServer((host, port), handler(reviewer))
    print(f"paoding review: http://{host}:{server.server_address[1]}/  (Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
