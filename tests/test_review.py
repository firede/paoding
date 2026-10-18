"""Review server and inspect: what the reviewer sees and what the agent gets."""

import json
import os
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from paoding import diagnose, record, review
from paoding.review import Reviewer, handler

from test_core import mmah, outline_font, outline_segments, require_file  # noqa: F401  (fixtures)

CHARS = "申好国"


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory, outline_font, outline_segments, mmah):
    out = tmp_path_factory.mktemp("run")
    refs = [require_file("data/mmah/dictionary.txt"), require_file("data/mmah/graphics.txt")]
    params = {"level": 1, "ppem": None, "threshold": 0.85, "overrides": []}
    with open(out / "records.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(record.header(outline_font, refs, params), ensure_ascii=False) + "\n")
        for c in CHARS:
            f.write(json.dumps(record.to_record(outline_segments[c], outline_font), ensure_ascii=False) + "\n")
    return out


@pytest.fixture(scope="module")
def reviewer(outline_font, run_dir, mmah):
    return Reviewer(outline_font, outline_font.path, [run_dir], mmah)


def test_index_entries(reviewer):
    d = reviewer.index(0)
    assert d["viewbox"] == "0 -880 1000 1000"
    entries = {e["c"]: e for e in d["entries"]}
    assert list(entries) == list(CHARS)
    assert entries["好"]["s"] == "lr" and entries["好"]["g"] == 1
    assert [n[:2] for n in entries["好"]["n"]] == [["0", "女"], ["1", "子"]]
    assert all(score is not None for _, _, score in entries["申"]["n"])


def test_skeleton_and_flags(reviewer):
    sk = reviewer.skeleton("申")
    assert sk["segments"] and all(d.startswith("M") for d in sk["segments"])
    assert len(sk["joints"]) >= 4 and all(len(j) == 3 and j[2] > 0 for j in sk["joints"])   # 丨 through 日
    assert reviewer.glyph(0, "申")["flags"] == reviewer.runs[0].records["申"].get("flags", [])
    assert all("f" in e for e in reviewer.index(0)["entries"])
    with pytest.raises(KeyError):
        reviewer.skeleton("\U000E0000")


def test_part_label_nested():
    assert review.part_label({"ids": "⿱⿰日召灬"}, "0") == "⿰日召"
    assert review.part_label({"ids": "⿱⿰日召灬"}, "0.1") == "召"
    assert review.part_label({"ids": "⿱⿰日召灬"}, "5") == "？"


def test_glyph_matches_record(reviewer):
    g = reviewer.glyph(0, "申")
    rec = reviewer.runs[0].records["申"]
    assert set(g["flat"]) == set(g["overlay"]) == set(rec["nodes"])
    assert all(d.startswith("M") for d in g["flat"].values())
    assert len(g["cuts"]) == sum(len(u.get("cuts", [])) for u in rec["units"]) > 0


def test_gaps(reviewer):
    d = reviewer.gaps(0)
    groups = {g["key"]: g["chars"] for g in d["groups"]}
    assert d["covered"] == len(CHARS)
    assert d["hanzi"] == d["covered"] + sum(len(cs) for cs in groups.values())
    assert "申" not in groups["not-run"] and "照" in groups["not-run"]
    assert groups["no-mmah"] and not any(c in reviewer.mmah for c in groups["no-mmah"][:50])


def test_run_from_another_font(run_dir, outline_font):
    with pytest.raises(ValueError, match="SHA-256"):
        review.Run(run_dir, "0" * 64)


def test_http(reviewer):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(reviewer))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "paoding review" in page
        runs = json.load(urllib.request.urlopen(base + "/api/runs"))
        assert runs["runs"][0]["count"] == len(CHARS)
        req = urllib.request.Request(base + "/api/glyphs", json.dumps({"run": 0, "chars": ["好", "照"]}).encode(),
                                     method="POST")
        assert list(json.load(urllib.request.urlopen(req))) == ["好"]
        assert json.load(urllib.request.urlopen(base + "/api/groups"))["groups"] == []
        req = urllib.request.Request(base + "/api/groups", json.dumps({"groups": [{"id": "x"}]}).encode(), method="POST")
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req)
        assert err.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(base + "/api/index?run=3")
        assert err.value.code == 400
    finally:
        server.shutdown()
        server.server_close()


def test_parse_targets():
    assert diagnose.parse_targets(["照", "蔗@512,300", "蔗@80.5,-90"]) == {"照": [], "蔗": [(512, 300), (80.5, -90)]}
    with pytest.raises(ValueError):
        diagnose.parse_targets(["照蔗"])


def test_inspect_char(tmp_path, outline_font, outline_segments, run_dir):
    seg = outline_segments["申"]
    shared = next(f for u in seg.units for f in u.faces if len(f.nodes) > 1)
    point = outline_font.from_em(*shared.point)
    run_rec = json.loads((run_dir / "records.jsonl").read_text(encoding="utf-8").splitlines()[1])
    info = diagnose.inspect_char(seg, outline_font, [point, (2000, 2000)], tmp_path, run_rec)
    assert (tmp_path / "panels.png").stat().st_size > 0
    assert json.loads((tmp_path / "inspect.json").read_text(encoding="utf-8"))["char"] == "申"
    assert [n["part"] for n in info["nodes"]] == ["日", "丨"]
    assert len(info["strokes"]) == 5 and all(s["source"] for s in info["strokes"])
    hit, miss = info["at"]
    assert hit["face"]["nodes"] == ["0", "1"] and hit["face"]["rule"] == "crossing"
    assert miss["unit"] is None and miss["nearest_strokes"]
    assert info["run"] == {"matches": True, "differs": []}


def test_run_rereads_changed_records(tmp_path, run_dir, outline_font, mmah):
    (tmp_path / "records.jsonl").write_bytes((run_dir / "records.jsonl").read_bytes())
    reviewer = Reviewer(outline_font, outline_font.path, [tmp_path], mmah)
    before = reviewer.runs_info()[0]["version"]
    assert len(reviewer.index(0)["entries"]) == len(CHARS)
    glyph = reviewer.glyph(0, "好")
    lines = (run_dir / "records.jsonl").read_text(encoding="utf-8").splitlines()
    (tmp_path / "records.jsonl").write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")
    os.utime(tmp_path / "records.jsonl", ns=(1, 1))   # a different mtime, however fast the rewrite
    assert reviewer.runs_info()[0]["version"] != before
    assert [e["c"] for e in reviewer.index(0)["entries"]] == [CHARS[0]]
    assert reviewer.glyph(0, CHARS[0]) is not glyph


def test_groups_round_trip(tmp_path, reviewer):
    reviewer.groups_path = tmp_path / "sub" / "groups.json"
    assert reviewer.groups() == []
    saved = reviewer.save_groups([
        {"id": "b", "name": "", "items": [{"run": "out/x", "char": "好", "node": "1", "at": [[1, 2]]}, {"char": "申"}]},
        {"id": "a", "name": "first", "created": "2026-10-19T10:00:00", "items": []},
    ])
    assert [g["id"] for g in reviewer.groups()] == ["b", "a"]   # kept in the order given
    assert reviewer.groups() == saved
    assert saved[0]["name"] == "b" and saved[0]["items"][1] == {"run": "", "char": "申", "node": None, "at": []}
    assert saved[0]["items"][0]["at"] == [[1.0, 2.0]]
    with pytest.raises(KeyError):
        reviewer.save_groups([{"id": "c"}])
    reviewer.groups_path = None
