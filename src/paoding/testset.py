"""Pick a test set that covers every IDS structure and the known hard cases.

Selection uses only the reference structure (and the font's coverage), so the
same set serves every target font. Picks are deterministic for a seed.
"""

import random

import shapely

from . import ids

QUOTAS = {"⿰": 25, "⿱": 25, "⿲": 10, "⿳": 10, "⿴": 10, "⿵": 12, "⿶": 8, "⿷": 10,
          "⿸": 15, "⿹": 10, "⿺": 15, "⿻": 20, "whole": 10}
# surrounding / enclosing parts that deserve their own samples
REQUIRED_PARTS = {"辶": 5, "廴": 3, "囗": 5, "门": 3, "走": 3, "疒": 2, "匚": 2, "凵": 2, "勹": 2, "戈": 3}
EXTRA = {"crossing": 15, "inferred": 8, "deep": 5, "non-gb2312": 5}


def gb2312_level(char):
    try:
        b = char.encode("gb2312")
    except UnicodeEncodeError:
        return 0
    return 1 if 0xB0 <= b[0] <= 0xD7 else 2 if 0xD8 <= b[0] <= 0xF7 else 0


def tags(ref):
    out = set()
    tree = ref.tree
    if ids.is_leaf(tree):
        out.add("whole")
        return out
    out.add(tree[0])
    if ids.depth(tree) >= 3:
        out.add("deep")
    if any(s != "matches" for s in ref.sources):
        out.add("inferred")
    for part in tree[1]:
        if ids.is_leaf(part) and part in REQUIRED_PARTS:
            out.add(part)
    if _reference_crossing(ref):
        out.add("crossing")
    return out


def _reference_crossing(ref, min_area=150.0):
    """Do strokes of different top-level parts overlap in the reference?"""
    polys = [shapely.make_valid(shapely.Polygon(s)) for s in ref.strokes]
    top = [p[:1] for p in ref.paths]
    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            if top[i] != top[j] and polys[i].intersection(polys[j]).area > min_area:
                return True
    return False


def select(mmah, font, seed=1):
    pool = [c for c in mmah.chars() if font.has(c)]
    level = {c: gb2312_level(c) for c in pool}
    common = sorted((c for c in pool if level[c] == 1), key=ord)
    rng = random.Random(seed)
    cache = {}

    def tag(c):
        if c not in cache:
            cache[c] = tags(mmah.reference(c))
        return cache[c]

    picked = {}

    def take(cands, n, label):
        cands = [c for c in cands if c not in picked]
        rng.shuffle(cands)
        for c in cands[:n]:
            picked[c] = label

    # structure quotas first, from common characters (tags of the first
    # character of the IDS are cheap: no geometry needed)
    by_op = {}
    for c in common:
        d = mmah.dictionary[c]["decomposition"]
        by_op.setdefault(d[0] if d[0] in ids.ARITY else "whole", []).append(c)
    for op, n in QUOTAS.items():
        cands = by_op.get(op, [])
        if len(cands) < n:  # rare structures: widen to the whole pool
            cands = cands + [c for c in pool if mmah.dictionary[c]["decomposition"][0] == op and c not in cands]
        take(cands, n, op)
    for part, n in REQUIRED_PARTS.items():
        have = sum(part in tag(c) for c in picked)
        take([c for c in common if part in mmah.dictionary[c]["decomposition"][1:3] and part in tag(c)],
             max(0, n - have), part)
    sample = rng.sample(common, min(len(common), 1500))
    for label, n in EXTRA.items():
        if label == "non-gb2312":
            take(sorted((c for c in pool if level[c] == 0), key=ord), n, label)
            continue
        have = sum(label in tag(c) for c in picked)
        take([c for c in sample if label in tag(c)], max(0, n - have), label)
    return [(c, sorted(tag(c) | {label})) for c, label in picked.items()]


def write(rows, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write("# char\ttags\n")
        for c, t in rows:
            f.write(f"{c}\t{','.join(t)}\n")


def read(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip() and not line.startswith("#"):
                out.append(line.split("\t")[0])
    return out
