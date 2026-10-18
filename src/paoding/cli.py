"""Command line: paoding run | review | inspect | testset | fetch."""

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
    tile: object = None
    error: str = ""
    secs: float = 0.0


def _init(font_path, ppem, mmah_root, override_paths, level, threshold, tiles="all"):
    """tiles: which contact-sheet tiles workers draw and send back: all | low | none."""
    from .target import open_target
    overrides = ov.load(override_paths)
    _worker.update(font=open_target(font_path, ppem), mmah=MMAH(mmah_root, overrides["structure"]),
                   overrides=overrides, level=level, threshold=threshold, tiles=tiles)


def _one(char):
    w = _worker
    o = ov.for_char(w["overrides"], char)
    t = time.time()
    try:
        run = segment_bitmap if w["font"].kind == "bitmap" else segment
        seg = run(char, w["font"], w["mmah"], level=w["level"], override=o, threshold=w["threshold"])
        # a tile is a few hundred KB: on large runs keep only the ones the sheets show
        drawn = w["tiles"] == "all" or (w["tiles"] == "low" and seg.status == "low")
        return Outcome(char, seg.ids, seg.status, seg.confidence, seg.metrics, seg.issues,
                       record.to_record(seg, w["font"]), report.tile(seg) if drawn else None, secs=time.time() - t)
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
    override_paths = ([] if args.no_builtin_overrides else [ov.BUILTIN]) + args.overrides
    init = (args.font, args.ppem, args.mmah, override_paths, args.level, args.threshold)
    _init(*init)
    font, mmah = _worker["font"], _worker["mmah"]
    excluded = {c for c, g in _worker["overrides"]["glyph"].items() if g.get("status") == "exclude"}
    chars = [c for c in chars if c in mmah and font.has(c) and c not in excluded]
    tiles = "none" if args.no_report else "all" if args.all_sheets or len(chars) <= 400 else "low"
    _worker["tiles"] = tiles

    results = []
    if args.jobs > 1:
        with ProcessPoolExecutor(args.jobs, initializer=_init, initargs=init + (tiles,)) as pool:
            results = list(pool.map(_one, chars, chunksize=8))
    else:
        results = [_one(c) for c in chars]

    failed = [r for r in results if r.error]
    ok = [r for r in results if not r.error]
    params = {"level": args.level, "ppem": args.ppem, "threshold": args.threshold, "overrides": _override_names(override_paths)}
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
        sheet_segs = [s for s in segs if s.tile is not None]
        report.sheets([s.tile for s in sorted(sheet_segs, key=lambda s: s.confidence)], str(rep / "sheet"))
        (rep / "summary.md").write_text(report.summary(segs, None, args.threshold), encoding="utf-8")
        (rep / "low.tsv").write_text(report.low_list(segs), encoding="utf-8")
    secs = sum(r.secs for r in results)
    auto = sum(s.status in ("auto", "verified") for s in segs)
    print(f"{len(segs)} characters, {auto} auto, {len(segs) - auto} low, {len(failed)} failed; "
          f"{secs:.1f}s compute -> {out}")
    for r in failed:
        print(f"  failed {r.char}: {r.error}", file=sys.stderr)
    return 1 if failed else 0


def _override_names(paths):
    """How the header lists override files: the builtin one as paoding:<name>."""
    return [("paoding:" if p == ov.BUILTIN else "") + Path(p).name for p in paths]


def _outline_font(path):
    from .target import open_target
    font = open_target(path)
    if font.kind != "outline":
        raise SystemExit(f"{path}: only outline fonts are supported here")
    return font


def cmd_review(args):
    from . import review
    font = _outline_font(args.font)
    mmah_dir = Path(args.mmah)
    mmah = MMAH(mmah_dir) if (mmah_dir / "dictionary.txt").is_file() and (mmah_dir / "graphics.txt").is_file() else None
    try:
        reviewer = review.Reviewer(font, args.font, args.runs, mmah, args.groups)
    except (OSError, ValueError) as err:
        raise SystemExit(f"paoding review: {err}")
    review.serve(reviewer, args.host, args.port)


def cmd_inspect(args):
    from . import diagnose
    from .review import sha256
    try:
        targets = diagnose.parse_targets(args.targets)
    except ValueError as err:
        raise SystemExit(f"paoding inspect: {err}")
    params, run_recs = {}, {}
    if args.run:
        header, run_recs = diagnose.run_records(args.run)
        params = header["params"]
        if header["font"]["sha256"] != sha256(args.font):
            print(f"warning: {args.run} was made from another version of {header['font']['file']}", file=sys.stderr)
    level = args.level or params.get("level", 1)
    threshold = args.threshold or params.get("threshold", DEFAULT_THRESHOLD)
    builtin = not args.no_builtin_overrides and ("overrides" not in params or
                                                 _override_names([ov.BUILTIN])[0] in params["overrides"])
    override_paths = ([ov.BUILTIN] if builtin else []) + args.overrides
    missing = set(params.get("overrides", [])) - set(_override_names(override_paths))
    if missing:
        print(f"warning: the run used overrides not given here: {', '.join(sorted(missing))} (pass --overrides)",
              file=sys.stderr)
    font = _outline_font(args.font)
    overrides = ov.load(override_paths)
    mmah = MMAH(args.mmah, overrides["structure"])
    out = Path(args.out)
    for char, points in targets.items():
        if char not in mmah or not font.has(char):
            print(f"{char}: not in {'MMAH' if char not in mmah else 'the font'}, skipped", file=sys.stderr)
            continue
        seg = segment(char, font, mmah, level=level, override=ov.for_char(overrides, char), threshold=threshold)
        info = diagnose.inspect_char(seg, font, points, out / char, run_recs.get(char) if args.run else None)
        weakest = min(info.get("nodes", []), key=lambda n: min(n.get("median_in", 1), n.get("purity", 1)), default=None)
        line = f"{char} {info['ids']} {info['status']} {info['confidence']:.3f}"
        if weakest:
            line += f"  weakest {weakest['node']} {weakest['part']} (in {weakest.get('median_in', 0):.2f}, purity {weakest.get('purity', 0):.2f})"
        if "run" in info:
            line += "  same as run" if info["run"]["matches"] else f"  differs from run: {', '.join(info['run']['differs'])}"
        elif args.run:
            line += "  not in run"
        print(f"{line}  -> {out / char}/")
    return 0


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
    r.add_argument("--no-builtin-overrides", action="store_true",
                   help="skip the structure corrections that ship with paoding")
    r.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    r.add_argument("--out", default="out/run")
    r.add_argument("--jobs", type=int, default=1)
    r.add_argument("--no-report", action="store_true")
    r.add_argument("--all-sheets", action="store_true", help="contact sheets for every character, not just low ones")
    r.set_defaults(func=cmd_run)

    v = sub.add_parser("review", help="serve a page to look through runs and copy what looks wrong")
    v.add_argument("runs", nargs="+", help="run directories (each with records.jsonl)")
    v.add_argument("--font", required=True, help="the OTF/TTF the runs were made from")
    v.add_argument("--mmah", default="data/mmah", help="to tell characters MMAH lacks from ones not run")
    v.add_argument("--groups", default="out/review-groups.json", help="where saved groups of selections are kept")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8417)
    v.set_defaults(func=cmd_review)

    i = sub.add_parser("inspect", help="rerun characters and write diagnostic panels and data")
    i.add_argument("targets", nargs="+", help="characters, each optionally with a point in font units: 照 蔗@512,300")
    i.add_argument("--font", required=True, help="OTF/TTF")
    i.add_argument("--run", help="run directory: use its level, threshold and builtin overrides, and compare results")
    i.add_argument("--mmah", default="data/mmah")
    i.add_argument("--level", type=int, help="default: the run's, else 1")
    i.add_argument("--threshold", type=float)
    i.add_argument("--overrides", action="append", default=[], help="override TOML; repeatable")
    i.add_argument("--no-builtin-overrides", action="store_true")
    i.add_argument("--out", default="out/inspect")
    i.set_defaults(func=cmd_inspect)

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
