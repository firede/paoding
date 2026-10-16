"""Make Me a Hanzi as a structural reference.

Only used while segmenting: decompositions, stroke-to-part matches, and stroke
geometry (outlines and medians) guide the split of the target glyph. None of
the MMAH geometry is written to any output.

MMAH graphics use a 1024 grid, y up, ascent 900 and descent 124; they are
mapped into the em frame (1000 units, -120..880).
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.optimize import linear_sum_assignment

from . import ids
from .geometry import FlattenPen, draw_svg


def to_em(x, y):
    return x * 1000 / 1024, (y + 124) * 1000 / 1024 - 120


@dataclass
class Reference:
    char: str
    ids: str
    tree: object
    strokes: list            # flattened stroke outlines, em frame
    medians: list            # stroke medians, em frame
    paths: list              # node path of each stroke (tuple)
    sources: list            # how each path was decided: matches | unknown-leaf | nearest | override
    radical: str = ""
    issues: list = field(default_factory=list)
    notes: list = field(default_factory=list)   # components left unexpanded, and why

    def nodes(self, level):
        """Parts at a split level and each stroke's index into them.

        Level 1 is the top-level split. A stroke whose path is shorter than the
        level (it matches a leaf higher up) keeps its shorter node.
        """
        cut = [p[:level] for p in self.paths]
        nodes = sorted(set(cut))
        index = {n: i for i, n in enumerate(nodes)}
        return nodes, [index[c] for c in cut]


class MMAH:
    def __init__(self, root, structures=None):
        """structures: structure overrides by character, used for components met while expanding."""
        self.structures = structures or {}
        root = Path(root)
        self.dictionary, self.graphics = {}, {}
        with open(root / "dictionary.txt", encoding="utf-8") as f:
            for line in f:
                e = json.loads(line)
                self.dictionary[e["character"]] = e
        with open(root / "graphics.txt", encoding="utf-8") as f:
            for line in f:
                e = json.loads(line)
                self.graphics[e["character"]] = e

    def __contains__(self, char):
        return char in self.dictionary and char in self.graphics

    def chars(self):
        return [c for c in self.dictionary if c in self.graphics]

    def reference(self, char, override=None, depth=1, _seen=()):
        """Build the reference for one character; override may replace ids and matches.

        MMAH decomposes a character one or two levels deep; parts that are
        themselves characters are expanded from their own entries until the
        tree reaches `depth` (see _expand).
        """
        e, g = self.dictionary[char], self.graphics[char]
        override = override or {}
        decomposition = override.get("ids", e["decomposition"])
        matches = override.get("matches", e["matches"])
        strokes, medians = [], []
        for d in g["strokes"]:
            pen = FlattenPen()
            draw_svg(d, pen, to_em)
            strokes.append(np.array(pen.contours[0]))
        for m in g["medians"]:
            medians.append(np.array([to_em(x, y) for x, y in m]))

        ref = Reference(char, decomposition, None, strokes, medians, [], [], e.get("radical", ""))
        if decomposition[0] in ids.ARITY:
            try:
                ref.tree = ids.parse(decomposition)
            except ValueError as err:
                ref.issues.append(str(err))
        if ref.tree is not None and all(leaf == ids.UNKNOWN for _, leaf in ids.leaf_paths(ref.tree)):
            # MMAH saw parts but named none of them: nothing to split by
            ref.notes.append(f"{decomposition}: no known parts, kept whole")
            ref.tree, ref.ids = None, ids.UNKNOWN
            decomposition = ids.UNKNOWN
        if ref.tree is None:
            # whole character (no decomposition, or one we cannot read)
            ref.tree = decomposition
            ref.paths = [()] * len(strokes)
            ref.sources = ["whole"] * len(strokes)
            return ref

        src = "override" if "matches" in override else "matches"
        # strokes MMAH leaves unmatched belong to its unknown parts; with several
        # unknown parts, to the deepest node holding all of them
        unknown = _unknown_leaves(ref.tree)
        common = _common_prefix(unknown)
        for m in matches:
            path = tuple(m) if m is not None else None
            if path is not None and not _valid(ref.tree, path):
                ref.issues.append(f"match {m} is not a node of {decomposition}")
                path = None
            if path is not None:
                ref.paths.append(path); ref.sources.append(src)
            elif unknown and (len(unknown) == 1 or common):
                ref.paths.append(common); ref.sources.append("unknown-leaf")
            else:
                ref.paths.append(None); ref.sources.append("nearest")
        if len(ref.paths) != len(strokes):
            ref.issues.append(f"{len(ref.paths)} matches for {len(strokes)} strokes")
            ref.paths = (ref.paths + [None] * len(strokes))[:len(strokes)]
            ref.sources = (ref.sources + ["nearest"] * len(strokes))[:len(strokes)]
        for i, p in enumerate(ref.paths):
            if p is None:
                known = [j for j, q in enumerate(ref.paths) if q is not None]
                if not known:
                    ref.paths[i] = ()
                    continue
                j = min(known, key=lambda j: _polyline_gap(medians[i], medians[j]))
                ref.paths[i] = ref.paths[j]
        used = {p[:1] for p in ref.paths}
        for k, child in enumerate(ref.tree[1]):
            if (k,) not in used:
                ref.issues.append(f"part {k} ({ids.leaves(child)}) has no strokes")
        self._expand(ref, depth, _seen + (char,))
        return ref

    def basic(self, char):
        """Is char a basic component, not worth splitting further?

        MMAH decomposes even pictographs (口 as 冂 + 一, 木 as 十 + 八), so a
        component counts as basic when MMAH calls it pictographic, when its
        parts interweave (⿻), or when a part is unknown or a single stroke.
        A structure override with a non-IDS decomposition also makes it basic.
        """
        e = self.dictionary[char]
        d = self.structures.get(char, {}).get("ids", e["decomposition"])
        if d[0] not in ids.ARITY or d[0] == "⿻":
            return True
        if (e.get("etymology") or {}).get("type") == "pictographic":
            return True
        try:
            tree = ids.parse(d)
        except ValueError:
            return True
        return any(c == ids.UNKNOWN or (c in self.graphics and len(self.graphics[c]["strokes"]) == 1)
                   for c in tree[1] if ids.is_leaf(c))

    def _expand(self, ref, depth, seen):
        """Replace leaf components shallower than depth by their own decomposition.

        The component's strokes here are matched to its strokes as a character
        by shape (medians normalised to their box), which tolerates a different
        stroke order; a component whose stroke count or shapes do not line up
        stays a leaf.
        """
        for path, leaf in ids.leaf_paths(ref.tree):
            if len(path) >= depth or leaf == ids.UNKNOWN or leaf in seen or leaf not in self or self.basic(leaf):
                continue
            here = [i for i, p in enumerate(ref.paths) if p == path]
            if not here:
                continue
            sub = self.reference(leaf, self.structures.get(leaf), depth - len(path), seen)
            if ids.is_leaf(sub.tree):
                continue
            if sub.issues:
                ref.notes.append(f"{leaf}: not expanded ({sub.issues[0]})")
                continue
            if len(sub.medians) != len(here):
                ref.notes.append(f"{leaf}: not expanded ({len(here)} strokes here, {len(sub.medians)} alone)")
                continue
            order, cost = _match_strokes([ref.medians[i] for i in here], sub.medians)
            if cost > 0.25:
                ref.notes.append(f"{leaf}: not expanded (strokes differ, {cost:.2f})")
                continue
            ref.tree = ids.replace(ref.tree, path, sub.tree)
            for i, j in zip(here, order):
                ref.paths[i] = path + sub.paths[j]
                if sub.sources[j] not in ("matches", "whole"):
                    ref.sources[i] = sub.sources[j]
            ref.notes += [f"{leaf}: {n}" for n in sub.notes]
        ref.ids = ids.unparse(ref.tree)


def _match_strokes(a, b, n=16):
    """Best one-to-one match of stroke medians a to b, each set normalised to its
    own box. Returns (index into b for each stroke of a, mean point distance)."""
    def norm(ms):
        pts = np.concatenate(ms)
        lo, size = pts.min(0), max(np.ptp(pts, axis=0).max(), 1.0)
        return [_resample_n((m - lo) / size, n) for m in ms]
    a, b = norm(a), norm(b)
    cost = np.array([[np.linalg.norm(x - y, axis=1).mean() for y in b] for x in a])
    rows, cols = linear_sum_assignment(cost)
    order = cols[np.argsort(rows)]
    return order, float(cost[rows, cols].mean())


def _resample_n(line, n):
    seg = np.linalg.norm(np.diff(line, axis=0), axis=1)
    t = np.concatenate([[0], np.cumsum(seg)])
    if t[-1] == 0:
        return np.repeat(line[:1], n, axis=0)
    s = np.linspace(0, t[-1], n)
    return np.column_stack([np.interp(s, t, line[:, 0]), np.interp(s, t, line[:, 1])])


def _unknown_leaves(tree, path=()):
    if ids.is_leaf(tree):
        return [path] if tree == ids.UNKNOWN else []
    return [p for i, c in enumerate(tree[1]) for p in _unknown_leaves(c, path + (i,))]


def _common_prefix(paths):
    if not paths:
        return ()
    out = []
    for parts in zip(*paths):
        if len(set(parts)) > 1:
            break
        out.append(parts[0])
    return tuple(out)


def _valid(tree, path):
    for i in path:
        if ids.is_leaf(tree) or i >= len(tree[1]):
            return False
        tree = tree[1][i]
    return True


def _polyline_gap(a, b):
    return float(np.min(np.linalg.norm(a[:, None] - b[None], axis=-1)))
