"""Review server: look through runs, select what looks wrong, copy it for an agent.

Glyphs are rebuilt from records.jsonl and the font with record.realize_record,
so the page shows exactly the geometry a consumer of the records gets. The
reviewer only looks and selects; the copied text names the run, the font and
the characters (optionally a part and a point in font units), with the
`paoding inspect` command that reproduces them.
"""

import hashlib
import json
import threading
import unicodedata
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import ids
from .geometry import svg_d
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
        self.path = str(path)
        lines = (Path(path) / "records.jsonl").read_text(encoding="utf-8").splitlines()
        self.header = json.loads(lines[0])
        if self.header.get("kind") != "header":
            raise ValueError(f"{path}: records.jsonl has no header")
        if self.header["font"]["sha256"] != font_sha:
            raise ValueError(f"{path}: made from {self.header['font']['file']} with another SHA-256 than --font")
        self.records = {}
        for line in lines[1:]:
            rec = json.loads(line)
            if "units" not in rec:
                raise ValueError(f"{path}: review covers outline fonts only ({rec['char']} has no units)")
            self.records[rec["char"]] = rec


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
    }


class Reviewer:
    def __init__(self, font, font_path, run_paths, mmah=None):
        self.font, self.font_path = font, str(font_path)
        sha = sha256(font_path)
        self.font_info = {"path": self.font_path, "file": Path(font_path).name, "sha256": sha}
        self.runs = [Run(p, sha) for p in run_paths]
        self.mmah = mmah
        self.lock = threading.Lock()   # fontTools glyph sets load lazily and are not thread-safe
        self.glyph = lru_cache(maxsize=8192)(self._glyph)

    def runs_info(self):
        return [{"path": r.path, "level": r.header["params"].get("level"),
                 "overrides": r.header["params"].get("overrides", []), "count": len(r.records)}
                for r in self.runs]

    def index(self, k):
        upm = self.font.units_per_em
        descender = self.font.from_em(0, -120)[1]
        run = self.runs[k]
        return {"font": self.font_info, "run": self.runs_info()[k], "colors": ["#%02x%02x%02x" % c for c in COLORS],
                "viewbox": f"0 {-(upm + descender):g} {upm:g} {upm:g}", "entries": [entry(r) for r in run.records.values()]}

    def _glyph(self, k, char):
        rec = self.runs[k].records[char]
        with self.lock:
            views = realize_record(rec, self.font)
        cuts = ["M" + " L".join(f"{x:g} {y:g}" for x, y in line) for u in rec["units"] for line in u.get("cuts", [])]
        return {v: {n: svg_d(p) for n, p in d.items()} for v, d in views.items()} | {"cuts": cuts}

    def gaps(self, k):
        """The font's hanzi without a record in this run, by why."""
        have = self.runs[k].records
        missing = [chr(c) for c in sorted(self.font.cmap) if is_hanzi(chr(c)) and chr(c) not in have]
        if self.mmah is None:
            groups = [("not-run", "不在本次运行中", missing)]
        else:
            groups = [("no-mmah", "MMAH 没有此字，无结构数据", [c for c in missing if c not in self.mmah]),
                      ("not-run", "MMAH 有此字，但不在本次运行中（未选入、失败或被排除）",
                       [c for c in missing if c in self.mmah])]
        return {"hanzi": len(have) + len(missing), "covered": len(have),
                "groups": [{"key": key, "label": label, "chars": "".join(cs)} for key, label, cs in groups]}


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
                else:
                    self.send({"error": "not found"}, status=404)
            except (KeyError, ValueError) as err:
                self.send({"error": str(err)}, status=400)

        def do_POST(self):
            url = urlsplit(self.path)
            try:
                if url.path != "/api/glyphs":
                    self.send({"error": "not found"}, status=404)
                    return
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                k = self.run_index({"run": [str(body.get("run", 0))]})
                records = reviewer.runs[k].records
                self.send({c: reviewer.glyph(k, c) for c in body.get("chars", []) if c in records})
            except (KeyError, ValueError) as err:
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
