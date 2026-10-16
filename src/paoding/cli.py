"""Command line: paoding run | testset."""

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import overrides as ov
from . import record, report, testset
from .mmah import MMAH
from .segment import DEFAULT_THRESHOLD, segment
from .segment_bitmap import segment_bitmap

_worker = {}


@dataclass
class Outcome:
    """What a worker sends back: everything the run writes, nothing unpicklable."""
    char: str
    ids: str = ""
    status: str = "failed"
    confidence: float = 0.0
    metrics: dict = field(default_factory=dict)
    issues: list = field(default_factory=list)
    record: dict = None
    page: dict = None
    tile: object = None
    error: str = ""
    secs: float = 0.0


def _init(font_path, ppem, mmah_root, override_paths, level, threshold):
    from .target import open_target
    _worker.update(font=open_target(font_path, ppem), mmah=MMAH(mmah_root), overrides=ov.load(override_paths),
                   level=level, threshold=threshold)


def _one(char):
    w = _worker
    o = ov.for_char(w["overrides"], char)
    t = time.time()
    try:
        run = segment_bitmap if w["font"].kind == "bitmap" else segment
        seg = run(char, w["font"], w["mmah"], level=w["level"], override=o, threshold=w["threshold"])
        return Outcome(char, seg.ids, seg.status, seg.confidence, seg.metrics, seg.issues,
                       record.to_record(seg, w["font"]), report.page_data(seg, w["font"]),
                       report.tile(seg), secs=time.time() - t)
    except Exception as err:  # keep the run going; the failure is reported
        return Outcome(char, error=f"{type(err).__name__}: {err}", secs=time.time() - t)


def cmd_run(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.testset:
        chars = testset.read(args.testset)
    elif args.chars:
        chars = list(dict.fromkeys(args.chars))
    else:
        chars = MMAH(args.mmah).chars()
    init = (args.font, args.ppem, args.mmah, args.overrides, args.level, args.threshold)
    _init(*init)
    font, mmah = _worker["font"], _worker["mmah"]
    excluded = {c for c, g in _worker["overrides"]["glyph"].items() if g.get("status") == "exclude"}
    chars = [c for c in chars if c in mmah and font.has(c) and c not in excluded]

    results = []
    if args.jobs > 1:
        with ProcessPoolExecutor(args.jobs, initializer=_init, initargs=init) as pool:
            results = list(pool.map(_one, chars, chunksize=8))
    else:
        results = [_one(c) for c in chars]

    failed = [r for r in results if r.error]
    ok = [r for r in results if not r.error]
    params = {"level": args.level, "ppem": args.ppem, "threshold": args.threshold, "overrides": [Path(p).name for p in args.overrides]}
    refs = [Path(args.mmah) / "dictionary.txt", Path(args.mmah) / "graphics.txt"]
    with open(out / "records.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(record.header(font, refs, params), ensure_ascii=False) + "\n")
        for r in ok:
            f.write(json.dumps(r.record, ensure_ascii=False, separators=(",", ":")) + "\n")
    segs = ok
    if not args.no_report:
        rep = out / "report"
        rep.mkdir(exist_ok=True)
        for old in rep.glob("sheet-*.png"):
            old.unlink()
        if not args.all_sheets and len(segs) > 400:
            sheet_segs = [s for s in segs if s.status == "low"]
        else:
            sheet_segs = segs
        report.sheets([s.tile for s in sorted(sheet_segs, key=lambda s: s.confidence)], str(rep / "sheet"))
        report.write_html(rep / "index.html", f"paoding · {Path(args.font).name}",
                          [r.page for r in ok], font.units_per_em, font.from_em(0, -120)[1])
        (rep / "summary.md").write_text(report.summary(segs, None, args.threshold), encoding="utf-8")
        (rep / "low.tsv").write_text(report.low_list(segs), encoding="utf-8")
    secs = sum(r.secs for r in results)
    auto = sum(s.status in ("auto", "verified") for s in segs)
    print(f"{len(segs)} characters, {auto} auto, {len(segs) - auto} low, {len(failed)} failed; "
          f"{secs:.1f}s compute -> {out}")
    for r in failed:
        print(f"  failed {r.char}: {r.error}", file=sys.stderr)
    return 1 if failed else 0


def cmd_testset(args):
    from .target.outline import OutlineFont
    rows = testset.select(MMAH(args.mmah), OutlineFont(args.font), seed=args.seed)
    testset.write(rows, args.out)
    print(f"{len(rows)} characters -> {args.out}")


def cmd_fetch(args):
    from .fetch import fetch
    fetch(args.sources, args.data, args.only)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="paoding", description="Split hanzi glyphs into IDS components.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="segment characters of a font")
    r.add_argument("--font", required=True, help="OTF/TTF, or bitmap: BDF, Unifont hex, OTB")
    r.add_argument("--ppem", type=int, help="bitmap strike size, or pixel size of a pixel-outline font")
    r.add_argument("--mmah", default="data/mmah", help="directory with dictionary.txt and graphics.txt")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--chars", help="characters to run")
    g.add_argument("--testset", help="test set file (char<TAB>...)")
    r.add_argument("--level", type=int, default=1)
    r.add_argument("--overrides", action="append", default=[], help="override TOML; repeatable")
    r.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    r.add_argument("--out", default="out/run")
    r.add_argument("--jobs", type=int, default=1)
    r.add_argument("--no-report", action="store_true")
    r.add_argument("--all-sheets", action="store_true", help="contact sheets for every character, not just low ones")
    r.set_defaults(func=cmd_run)

    t = sub.add_parser("testset", help="pick a test set covering every structure")
    t.add_argument("--font", required=True)
    t.add_argument("--mmah", default="data/mmah")
    t.add_argument("--seed", type=int, default=1)
    t.add_argument("--out", default="testsets/core200.tsv")
    t.set_defaults(func=cmd_testset)

    f = sub.add_parser("fetch", help="download the pinned inputs of sources.toml")
    f.add_argument("--sources", default="sources.toml")
    f.add_argument("--data", default="data")
    f.add_argument("only", nargs="*", help="entries to fetch (default: all)")
    f.set_defaults(func=cmd_fetch)

    args = ap.parse_args(argv)
    sys.exit(args.func(args) or 0)
