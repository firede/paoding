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
    def __init__(self, root):
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

    def reference(self, char, override=None):
        """Build the reference for one character; override may replace ids and matches."""
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
        return ref


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
