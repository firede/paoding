"""References for characters Make Me a Hanzi lacks, composed from their parts.

The structure comes from an IDS table (BabelStone's IDS.TXT). Each part's
strokes are taken from an MMAH character that has the same part in the same
place (玕 ⿰𤣩干: 𤣩 as the left of 玩, 干 as the right of 刊), so a part keeps
the shape it has in that position; a part never seen there is drawn from its
own entry and scaled into the box that position usually takes. Parts set side
by side are made to meet halfway where their sources overlap or leave a gap.
Registration onto the target glyph absorbs what this leaves approximate.

A place's part goes by MMAH's name and, where the table splits the character
the same way at the top, by the table's too: MMAH has 汤 as ⿰氵昜 and IDS.TXT
as ⿰氵𠃓, so 旸 ⿰日𠃓 finds its right part there.

Composed entries have MMAH's own form (dictionary decomposition and matches,
graphics strokes and medians), so the rest of paoding treats them alike.
"""

import re
from collections import defaultdict

import numpy as np

from . import ids

SIDE = {"⿰": 0, "⿲": 0, "⿱": 1, "⿳": 1}    # parts side by side: along x, or along y
GAP = 20                                       # between parts that are made to meet, MMAH units
_TOKEN = re.compile(r"[A-Za-z]|-?\d+(?:\.\d+)?")


def read_ids(path):
    """IDS.TXT -> {char: IDS}, preferring the PRC (G) source form."""
    out = {}
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            fields = line.rstrip("\r\n").split("\t")
            seqs = [m.groups() for s in fields[2:] if (m := re.fullmatch(r"\^(.*)\$\((\w+)\)", s))]
            g = [s for s, src in seqs if "G" in src] or [s for s, _ in seqs]
            if g:
                out[fields[1]] = g[0]
    return out


def read_tgh(path):
    """Unihan_OtherMappings.txt -> the 通用规范汉字表 (kTGH) characters, in the table's order."""
    found = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if "\tkTGH\t" in line:
                cp, _, value = line.rstrip("\n").split("\t")
                found.append((int(value.split()[0].split(":")[1]), chr(int(cp[2:], 16))))
    return [c for _, c in sorted(found)]


class Composer:
    def __init__(self, dictionary, graphics, table):
        self.graphics, self.table = graphics, table
        self.slots = defaultdict(list)    # (operator, k, part) -> [(char, its stroke indices, box)]
        self.boxes = defaultdict(list)    # (operator, k) -> [box]
        for c, e in dictionary.items():
            g = graphics.get(c)
            if g is None or len(e["matches"]) != len(g["medians"]) or e["decomposition"][0] not in ids.ARITY:
                continue
            try:
                op, kids = ids.parse(e["decomposition"])
            except ValueError:
                continue
            names = [{kid} if ids.is_leaf(kid) else set() for kid in kids]
            other = _parse(table.get(c))
            if other is not None and not ids.is_leaf(other) and other[0] == op:
                for k, kid in enumerate(other[1]):
                    if ids.is_leaf(kid):
                        names[k].add(kid)
            for k, kid in enumerate(kids):
                here = [i for i, m in enumerate(e["matches"]) if m and m[0] == k]
                if not here:
                    continue
                b = _box([g["medians"][i] for i in here])
                self.boxes[op, k].append(b)
                for name in names[k] - {ids.UNKNOWN}:
                    self.slots[op, k, name].append((c, here, b))

    def entry(self, char):
        """(dictionary entry, graphics entry) in MMAH's form, or None when the IDS is
        missing, whole, interwoven (⿻ and the like), or has a part with no strokes to draw."""
        tree = self._tree(_parse(self.table.get(char)), {char})
        if tree is None or ids.is_leaf(tree) or tree[0] == "⿻":
            return None
        got = self._node(tree)
        if got is None:
            return None
        matches, strokes, medians = got
        return ({"character": char, "decomposition": ids.unparse(tree), "matches": matches, "radical": ""},
                {"character": char, "strokes": strokes, "medians": medians})

    def _tree(self, tree, seen):
        """The tree with parts that cannot be drawn where they stand replaced by their
        own IDS; None if some part cannot be drawn at all."""
        if tree is None or ids.is_leaf(tree):
            return tree
        op, kids = tree[0], []
        for k, kid in enumerate(tree[1]):
            if ids.is_leaf(kid) and not self._drawable(op, k, kid) and kid not in seen:
                kid = self._tree(_parse(self.table.get(kid)), seen | {kid})
            elif not ids.is_leaf(kid):
                kid = self._tree(kid, seen)
            if kid is None or (ids.is_leaf(kid) and not self._drawable(op, k, kid)):
                return None
            kids.append(kid)
        return (op, kids)

    def _drawable(self, op, k, part):
        return bool(self.slots.get((op, k, part))) or (part in self.graphics and bool(self.boxes.get((op, k))))

    def _node(self, tree):
        op, kids = tree
        parts = []
        for k, kid in enumerate(kids):
            part = self._part(op, k, kid)
            if part is None:
                return None
            parts.append(part)
        if op in SIDE:
            _meet(parts, SIDE[op])
        matches, strokes, medians = [], [], []
        for k, part in enumerate(parts):
            matches += [[k] + m for m in part["matches"]]
            strokes += part["strokes"]
            medians += part["medians"]
        return matches, strokes, medians

    def _part(self, op, k, kid):
        """Part k of op: its matches below the part, strokes, medians and box."""
        seen = self.slots.get((op, k, kid)) if ids.is_leaf(kid) else None
        if seen:
            # the instance whose box is most typical of this part in this place
            mid = np.median([b for _, _, b in seen], axis=0)
            c, here, b = min(seen, key=lambda s: np.abs(s[2] - mid).sum())
            g = self.graphics[c]
            return {"matches": [[] for _ in here], "strokes": [g["strokes"][i] for i in here],
                    "medians": [g["medians"][i] for i in here], "box": b}
        if not self.boxes.get((op, k)):
            return None
        if ids.is_leaf(kid):
            g = self.graphics[kid]
            part = {"matches": [[] for _ in g["medians"]], "strokes": g["strokes"], "medians": g["medians"]}
        else:
            got = self._node(kid)
            if got is None:
                return None
            part = dict(zip(("matches", "strokes", "medians"), got))
        part["box"] = _box(part["medians"])
        _move(part, np.median(self.boxes[op, k], axis=0))
        return part


def _parse(text):
    if not text:
        return None
    try:
        return ids.parse(text.lstrip("〾"))
    except ValueError:
        return None


def _meet(parts, axis):
    """Neighbours from different characters may overlap or stand apart: move the
    shared edge halfway, shrinking a part only where it overlaps."""
    for a, b in zip(parts, parts[1:]):
        na, nb = a["box"].copy(), b["box"].copy()
        if axis == 0:
            edge = (na[2] + nb[0]) / 2
            na[2], nb[0] = min(na[2], edge - GAP / 2), max(nb[0], edge + GAP / 2)
        else:
            # y up: a is above b
            edge = (na[1] + nb[3]) / 2
            na[1], nb[3] = max(na[1], edge + GAP / 2), min(nb[3], edge - GAP / 2)
        for p, new in ((a, na), (b, nb)):
            if not np.allclose(p["box"], new):
                _move(p, new)


def _move(part, box):
    src = part["box"]
    k = (box[2:] - box[:2]) / np.maximum(src[2:] - src[:2], 1)
    fn = lambda x, y: (box[0] + (x - src[0]) * k[0], box[1] + (y - src[1]) * k[1])
    part["strokes"] = [_move_svg(d, fn) for d in part["strokes"]]
    part["medians"] = [[[round(v, 1) for v in fn(x, y)] for x, y in m] for m in part["medians"]]
    part["box"] = np.asarray(box, float)


def _move_svg(d, fn):
    toks, out, i = _TOKEN.findall(d), [], 0
    while i < len(toks):
        if toks[i].isalpha():
            out.append(toks[i])
            i += 1
        else:
            x, y = fn(float(toks[i]), float(toks[i + 1]))
            out += [f"{x:.1f}", f"{y:.1f}"]
            i += 2
    return " ".join(out)


def _box(medians):
    pts = np.concatenate([np.asarray(m, float) for m in medians])
    return np.concatenate([pts.min(0), pts.max(0)])
