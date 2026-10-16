"""Manual overrides, in TOML. Several files may be given; later ones win per key.

    # Font-independent fixes to the reference structure.
    [structure.赢]
    ids = "⿱吂⿲月贝凡"            # replaces the MMAH decomposition
    matches = [[0], [0], ...]      # replaces MMAH matches, one node path per stroke

    # Per-glyph fixes for one target font (keep these in a file per font).
    [glyph.申]
    stack = ["0", "1"]             # stacking order, bottom first (default: IDS order)
    units = { "3" = "1" }          # unit containing contour 3 goes wholly to node "1"
    flat = [{ point = [500, 420], node = "1" }]   # flat owner of the shared face at a point (font units)
    status = "verified"            # or "exclude"
    note = "free text"
"""

import tomllib


def load(paths):
    merged = {"structure": {}, "glyph": {}}
    for p in paths or []:
        with open(p, "rb") as f:
            data = tomllib.load(f)
        unknown = set(data) - set(merged)
        if unknown:
            raise ValueError(f"{p}: unknown sections {sorted(unknown)}")
        for section in merged:
            for char, entry in data.get(section, {}).items():
                merged[section].setdefault(char, {}).update(entry)
    return merged


def for_char(overrides, char):
    """The override dict segment() takes for one character."""
    g = overrides["glyph"].get(char, {})
    out = {k: g[k] for k in ("stack", "units", "flat", "status") if k in g}
    if char in overrides["structure"]:
        out["structure"] = overrides["structure"][char]
    return out
