# Annotation format

`paoding run` writes `records.jsonl`: one JSON object per line. The first line
is a header; each following line describes one character of one target font.

Records hold no glyph outlines and no reference (Make Me a Hanzi) geometry.
They refer to the target font by glyph name and contour index, and add only
cut lines and labelled points in the font's own units. To get outlines, rebuild
them from the record and your copy of the font (`paoding.record.realize_record`).

## Header

```json
{"kind": "header", "format": 1, "tool": "paoding 0.1.0.dev0",
 "font": {"file": "SourceHanSansSC-Regular.otf", "sha256": "f1d86111…"},
 "reference": [{"file": "dictionary.txt", "sha256": "…"}, {"file": "graphics.txt", "sha256": "…"}],
 "params": {"level": 1, "ppem": null, "threshold": 0.85, "overrides": []}}
```

A record is only valid for the exact font file named by the hash: contour
indices and coordinates change between font versions.

## Node paths

Parts are addressed by their path in the IDS tree, written with dots:
`""` is the whole character, `"0"` the first top-level part, `"1.0"` the first
part of the second. For `照 ⿱⿰日召灬`, `"0"` is 昭 and `"0.1"` is 召.
`level` says how deep the split goes; level 1 is the top-level split.

`ids` is the tree the paths refer to. Make Me a Hanzi decomposes a character
only a level or two deep (照 is `⿱昭灬`); below level 1, parts that are
characters themselves are expanded from their own entries (昭 is `⿰日召`), so
at level 3 照 is `⿱⿰日⿱刀口灬`. Expansion stops at basic components: those
MMAH calls pictographic (日, 木, 口), whose parts interweave (`⿻`), or that
have a single stroke or an unknown part among their parts. A structure override
can change where it stops. A part whose strokes do not line up with its own
entry (another stroke count, or other shapes) also stays whole. A character
whose MMAH parts are all unknown (門 is `⿰？？`) is not split at all; its
`ids` is `？`.

`nodes` lists the parts present at that level, in **stacking order, bottom
first** (IDS order unless overridden). A stroke whose reference path is
shorter than the level keeps its shorter node.

## Character record (outline fonts)

```json
{"char": "申", "codepoint": "U+7533", "glyph": "cid27077", "level": 1,
 "ids": "⿻日丨", "nodes": ["0", "1"],
 "status": "auto", "confidence": 0.95,
 "units": [
   {"contours": [0, 1, 2, 3, 4],
    "cuts": [[[538.7, 195.0], [537.9, 267.0]], …],
    "faces": [{"at": [715.6, 422.8], "nodes": ["0"], "flat": "0"},
              {"at": [497.6, 227.3], "nodes": ["0", "1"], "flat": "1", "rule": "crossing"},
              {"at": [497.0, 75.8], "nodes": ["1"], "flat": "1"}, …]}
 ],
 "metrics": {…}}
```

A **unit** is a connected piece of the glyph's ink: an outer boundary with its
holes. `contours` lists the glyph's original contour indices that make it up
(as a pen sees them via `glyphSet[glyph].draw`). Fonts with overlapping
contours work too: overlapping contours share a unit.

A unit either belongs wholly to one part:

```json
{"contours": [3], "node": "1"}            // add "override": true when forced by an override
```

or is cut. `cuts` are polylines in font units, each crossing the unit from
one side of its outline to the other. They split the unit into faces; each
entry of `faces` labels the face containing its point `at`:

- `nodes`: every part whose **overlay** includes the face;
- `flat`: the one part that owns the face in the **flat** view;
- `rule` (shared faces only): why `flat` won —
  `through` (its stroke passes through the junction, the other ends in it),
  `crossing` (both pass; the part higher in the stacking order wins),
  `corner` (neither passes; the part whose strokes run further inside wins,
  then the stroke drawn first), or `override`.

### Two views

- **Flat**: every face goes to its `flat` part. The parts tile the glyph
  exactly, without overlap. Use it for flat colouring, area statistics, or
  anything that paints each point once.
- **Overlay**: every face goes to all its `nodes`. Each part is complete on its
  own, so it can be shown alone (animation, highlighting, part search
  thumbnails); parts overlap at junctions. Painting overlay parts opaque in
  stacking order looks the same as the flat view except where a `through` or
  `corner` face is owned by a lower part.

At a T-junction the overlay extends the ending stroke into the stroke it meets,
up to where the reference stroke ends; the target glyph itself cannot tell how
deep that is, so treat the depth as approximate.

### Rebuilding the pieces

1. Unit outline: the union of its `contours`, overlaps removed.
2. Flatten the outline, add the cut lines (extended a few units past their
   ends so they cross it), and polygonize: the result is the unit's faces.
3. Label each face by the `faces` point inside it (nearest point if none).
4. For exact curves, clip the unit outline with each face grown by a few units
   minus all other faces. The cut edges are straight segments; everything else
   keeps the font's own curves.

`paoding.record.realize_record(record, font)` does this and returns
`{"overlay": {node: Path}, "flat": {node: Path}}` (skia-pathops, font units).

## Character record (bitmap fonts)

```json
{"char": "好", "glyph": "U+597D", …,
 "pixels": {"left": 0, "top": 14,
            "rows": ["...a............",
                     "...a....bbbbbb..",
                     …],
            "legend": {"a": {"nodes": ["0"], "flat": "0"},
                       "b": {"nodes": ["1"], "flat": "1"}}}}
```

`rows` covers the glyph's bitmap from the top row down; `left` and `top` place
column 0 and row 0 relative to the origin and baseline (pixels, y up). `.` is
no ink; a letter names the overlay set and flat owner of that pixel (a
junction pixel can carry a letter whose `nodes` lists several parts).

## Status and confidence

`status` is `auto` when `confidence` reaches the threshold, `low` otherwise,
or `verified` / `exclude` when an override says so. `confidence` is the
weakest part's score, lowered for inferred structure:

- `median_in`: share of a part's reference medians (ends trimmed) lying on or
  near the part's overlay ink — low when registration is off;
- `purity`: share of the skeleton of a part's flat ink that is not clearly
  nearer another part's strokes — low when ink went to the wrong part;
  For bitmap fonts, where every pixel goes to the nearest stroke, purity is
  instead the share of a part's straight pixel runs (three or more pixels)
  that no other part breaks into; a median may also stray half a pixel off
  its ink for `median_in`;
- `skel_cov`, `iou`: informative only; both are limited by the style gap
  between the reference (kaishu) and the target.

Penalties: ×0.95 for crossing/corner junctions, ×0.95 for strokes whose part
was inferred (not from MMAH matches or an override), ×0.5 when a part of the decomposition has no strokes, 0 when a
part ends up with no ink.

## Overrides

See `paoding.overrides` for the TOML layout: `[structure."<char>"]` fixes the
decomposition or matches for every font; `[glyph."<char>"]` sets, for one font,
the stacking order, whole-unit assignments by contour index, flat owners of
shared faces by point, and a review status.

## Provenance and licensing

- Contour indices and coordinates describe the target font; the font's
  license applies to anything rebuilt from it (for Source Han Sans: OFL, and
  derived fonts may not use the reserved name "Source").
- `ids`, `nodes` and the node paths come from Make Me a Hanzi's
  `dictionary.txt` (LGPL). Cut positions and face labels are computed with the
  help of its `graphics.txt` (Arphic Public License) but contain none of its
  outlines or medians.
