"""Parts as the target font draws them, gathered from its own confident splits.

A part drawn apart from the rest of a character (阝, 氵, 口 beside another
part) is one unit of ink, and a font draws it nearly the same in every
character that uses it in the same place. Where a split cut such a unit (the
reference's kaishu strokes reached onto it), units of the same shape that the
font gives wholly to that part in other characters say the whole unit is the
part's.

A part that touches another (言 in 誠, ⺮ in 等) is no unit of its own, but the
font often draws it standing free in other characters. Its ink there, as a
template, is fitted onto the character's ink: where it fits closely, the ink
it covers in the cut units is the part's.

`paoding run` does both in a second pass. The first pass gathers, from the
characters it is confident about (auto, no structure flags), the units it
gave wholly to one part and the parts it cut nowhere; characters with a cut
unit that matches such units of the unit's main owner, and flagged or low
characters with a cut part drawn free elsewhere, are split again.
"""

from collections import defaultdict

import numpy as np
from PIL import Image
from scipy.signal import fftconvolve

from .geometry import Canvas, transform_path

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


class Templates:
    """Parts the font draws standing free (no unit of them cut), by part label: (char, contours, box)."""

    def __init__(self, keep=8):
        self.keep = keep
        self.parts = defaultdict(list)

    def add(self, label, char, contours, box):
        self.parts[label].append((char, contours, box))

    def candidates(self, label, box, char):
        """The `keep` templates of a part whose boxes are most like `box`, from characters other than `char`."""
        found = [(_box_iou(b, box), c, cs) for c, cs, b in self.parts.get(label, ()) if c != char]
        return [(c, cs) for _, c, cs in sorted(found, key=lambda x: -x[0])[:self.keep]]


def _box_iou(a, b):
    w = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    h = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    size = lambda x: (x[2] - x[0]) * (x[3] - x[1])
    return w * h / max(size(a) + size(b) - w * h, 1e-9)


FIT = Canvas(256)
_PAD = 32


def fit(ink, part, reach=24, scales=(0.9, 0.94, 0.98, 1.02, 1.06, 1.1), off=4.0):
    """Place a template where most of it lies on the ink: moved at most `reach` pixels of FIT
    from where its own character has it, scaled a little on each axis.

    ink: mask on FIT, grown a pixel to forgive slightly thinner strokes. part: Path, em frame.
    Returns (share of the placed template on the ink, the placed Path).
    """
    mask = FIT.path_mask(part)
    ys, xs = np.nonzero(mask)
    if not len(ys):
        return 0.0, None
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    crop = Image.fromarray(mask[y0:y1, x0:x1].astype(np.uint8) * 255)
    ink = np.pad(ink.astype(float), _PAD)
    best = (-np.inf, 0.0, None)
    for sy in scales:
        for sx in scales:
            h, w = max(1, round((y1 - y0) * sy)), max(1, round((x1 - x0) * sx))
            t = np.asarray(crop.resize((w, h), Image.BILINEAR)) > 127
            # on[i, j]: template pixels on ink with its top left at (i, j) of the padded ink
            on = fftconvolve(ink, t[::-1, ::-1].astype(float), mode="valid")
            cy, cx = (y0 + y1 - h) / 2 + _PAD, (x0 + x1 - w) / 2 + _PAD
            ii, jj = np.ogrid[:on.shape[0], :on.shape[1]]
            # covering more ink, but each pixel off it costs `off` covered ones
            gain = np.where((abs(ii - cy) <= reach) & (abs(jj - cx) <= reach), on - off * (t.sum() - on), -np.inf)
            i, j = np.unravel_index(np.argmax(gain), gain.shape)
            if gain[i, j] > best[0]:
                best = (gain[i, j], float(on[i, j] / t.sum()), (i - _PAD, j - _PAD, h, w))
    _, share, place = best
    if place is None:
        return 0.0, None
    i, j, h, w = place
    # pixel box (x0, y0)-(x1, y1) of the template onto (j, i)-(j + w, i + h), in the em frame
    px, left, top = FIT.px, FIT.left, FIT.top
    kx, ky = w / (x1 - x0), h / (y1 - y0)
    move = lambda x, y: (left + j / px + (x - left - x0 / px) * kx, top - i / px - (top - y0 / px - y) * ky)
    return share, transform_path(part, move)
