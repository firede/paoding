"""Parts as the target font draws them, gathered from its own confident splits.

A part drawn apart from the rest of a character (阝, 氵, 口 beside another
part) is one unit of ink, and a font draws it nearly the same in every
character that uses it in the same place. Where a split cut such a unit (the
reference's kaishu strokes reached onto it), units of the same shape that the
font gives wholly to that part in other characters say the whole unit is the
part's.

`paoding run` does this as a second pass: the first pass gathers the units it
gave wholly to one part in characters it is confident about (auto, no
structure flags); characters with a cut unit that matches such units of the
unit's main owner are split again with that unit given to it whole.
"""

from collections import defaultdict

import numpy as np
from PIL import Image

from .geometry import Canvas

SIZE = 32          # side of the normalised shape raster
_CANVAS = Canvas(256)


def describe(path):
    """A unit's shape: its ink cropped to its box, resampled to SIZE x SIZE (packed bits), and the box size."""
    mask = _CANVAS.path_mask(path)
    ys, xs = np.nonzero(mask)
    if not len(ys):
        return None
    crop = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    small = np.asarray(Image.fromarray(crop.astype(np.uint8) * 255).resize((SIZE, SIZE), Image.BILINEAR)) > 127
    return np.packbits(small), (int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1))


class Library:
    """Shapes of units a font gives wholly to one part, by part label (its IDS, e.g. 阝 or ⿱冖口)."""

    def __init__(self, size_tol=0.08, iou=0.85, min_chars=2):
        self.size_tol, self.iou, self.min_chars = size_tol, iou, min_chars
        self.whole = defaultdict(list)     # label -> [(bits, (w, h), char)]

    def add(self, label, shape, char):
        if shape is not None:
            self.whole[label].append((shape[0], shape[1], char))

    def like(self, label, shape):
        """Characters with a unit wholly this part's and shaped like this one, if at least min_chars of them."""
        if shape is None:
            return []
        bits, (w, h) = shape
        mask = np.unpackbits(bits).astype(bool)
        chars = []
        for bits2, (w2, h2), char in self.whole.get(label, ()):
            if abs(w - w2) > self.size_tol * max(w, w2) + 1 or abs(h - h2) > self.size_tol * max(h, h2) + 1:
                continue
            other = np.unpackbits(bits2).astype(bool)
            if (mask & other).sum() >= self.iou * (mask | other).sum() and char not in chars:
                chars.append(char)
        return chars if len(chars) >= self.min_chars else []
