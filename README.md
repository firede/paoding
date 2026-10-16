# paoding

Split the hanzi of any font into the components of their Ideographic
Description Sequence — for colouring components, searching by component, or
showing one component at a time — without tying the result to the font the
structural data was drawn with.

[Make Me a Hanzi](https://github.com/skishore/makemeahanzi) (MMAH) describes
how each character decomposes and which stroke belongs to which component, but
its stroke geometry is that of one kaishu font. paoding registers those
strokes onto a target glyph and splits the target's own outlines (or pixels)
accordingly. The output is an annotation of the target font; MMAH geometry is
used only as a guide and never written out.

Status: early. Outline fonts split well at the top level and below
(`--level 2` and deeper); the bitmap path is in progress.

## Use

```sh
uv sync
uv run paoding fetch                     # pinned inputs into data/ (see sources.toml)
uv run paoding testset --font data/fonts/SourceHanSansSC-Regular.otf
uv run paoding run --font data/fonts/SourceHanSansSC-Regular.otf \
    --testset testsets/core200.tsv --out out/core200 --jobs 8
```

`run` writes `records.jsonl` (format: [docs/format.md](docs/format.md)) and a
report: `report/index.html` (switch flat/overlay views, show one component),
contact sheets `report/sheet-*.png` (lowest confidence first), `summary.md`
and `low.tsv`.

Bitmap fonts: pass a BDF, Unifont `.hex` or OpenType bitmap (`.otb`) file as
`--font`; for a pixel-outline OTF/TTF also pass `--ppem`.

Fix what the tool gets wrong with override files (`--overrides x.toml`,
repeatable): corrected decompositions for every font, and per-glyph
assignments, stacking order or review status for one font. See
`src/paoding/overrides.py`.

## How it works

1. **Reference.** MMAH gives the decomposition, a component path for each
   stroke, and stroke outlines and medians. For deeper levels, components are
   expanded from their own entries.
2. **Registration.** Fit the reference's ink box onto the target's, then a
   smooth non-rigid warp that pulls the medians onto the target's skeleton
   (matched on position and stroke direction, coarse to fine). Then, level by
   level, each component is registered again on its own: an affine fit to the
   ink it covered, then a gentler warp, so a component keeps its shape and its
   strokes are not dragged onto a neighbour's ink.
3. **Assignment.** The target's ink falls into units (an outer contour with its
   holes). A unit crossed by medians of a single component goes to it whole.
   Otherwise it is cut along bands around the registered strokes; where
   components meet, the face is shared, and the flat view gives it to the
   stroke that passes through (or, at a crossing, to the component stacked
   higher).
4. **Scoring.** Per component: how well its medians land on its ink, and how
   much of its ink sits clearly nearer another component's strokes.

## License

Code: MIT. Inputs keep their own licenses (sources.toml): MMAH dictionary data
LGPL, MMAH graphics Arphic Public License, Source Han Sans OFL, and so on.
