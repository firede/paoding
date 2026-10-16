"""Register MMAH stroke medians onto a target glyph.

1. Per-axis scale and shift mapping the reference ink box onto the target's.
2. Coarse-to-fine non-rigid refinement: a smooth displacement field (Gaussian
   RBFs on a grid over the em) solved by ICP between the medians and the
   target's skeleton. Points are matched on position and stroke direction, so
   a horizontal median does not snap onto a vertical stem.
"""

import numpy as np
from scipy.spatial import cKDTree
from skimage.morphology import skeletonize

from .geometry import EM_BOTTOM, EM_SIZE, resample


class Affine:
    def __init__(self, sx=1.0, sy=1.0, tx=0.0, ty=0.0):
        self.sx, self.sy, self.tx, self.ty = sx, sy, tx, ty

    def __call__(self, pts):
        pts = np.asarray(pts, float)
        return np.column_stack([pts[:, 0] * self.sx + self.tx, pts[:, 1] * self.sy + self.ty])


class RBFField:
    def __init__(self, grid):
        g = np.linspace(0, EM_SIZE, grid)
        self.centers = np.array([(x, y + EM_BOTTOM) for y in g for x in g])
        self.sigma = EM_SIZE / (grid - 1)
        self.theta = np.zeros((len(self.centers), 2))

    def basis(self, pts):
        d2 = ((pts[:, None, :] - self.centers[None]) ** 2).sum(-1)
        return np.exp(-d2 / (2 * self.sigma ** 2))

    def __call__(self, pts):
        return pts + self.basis(pts) @ self.theta


class Warp:
    """Affine followed by successive displacement fields."""

    def __init__(self, affine, fields=()):
        self.affine, self.fields = affine, list(fields)

    def __call__(self, pts):
        p = self.affine(pts)
        for f in self.fields:
            p = f(p)
        return p


def ink_box(mask, canvas):
    ys, xs = np.nonzero(mask)
    lo = canvas.pixel_centers(np.array([ys.max()]), np.array([xs.min()]))[0] - 0.5 / canvas.px
    hi = canvas.pixel_centers(np.array([ys.min()]), np.array([xs.max()]))[0] + 0.5 / canvas.px
    return lo, hi


def fit_box(src, dst, min_extent=60):
    """Per-axis scale+shift taking box src onto box dst; thin axes borrow the other scale."""
    (s0, s1), (d0, d1) = src, dst
    se, de = s1 - s0, d1 - d0
    scale = np.where(se > min_extent, de / np.maximum(se, 1e-6), np.nan)
    if np.isnan(scale).all():
        scale[:] = 1.0
    elif np.isnan(scale).any():
        scale[np.isnan(scale)] = scale[~np.isnan(scale)][0]
    t = (d0 + d1) / 2 - scale * (s0 + s1) / 2
    return Affine(scale[0], scale[1], t[0], t[1])


def _orientation_pca(pts, k=8):
    """Undirected tangent of each point from its neighbours, as (cos 2t, sin 2t)."""
    _, nb = cKDTree(pts).query(pts, k=min(k, len(pts)))
    q = pts[nb] - pts[nb].mean(1, keepdims=True)
    cov = np.einsum("nki,nkj->nij", q, q)
    _, v = np.linalg.eigh(cov)
    t = np.arctan2(v[:, 1, -1], v[:, 0, -1])
    return np.column_stack([np.cos(2 * t), np.sin(2 * t)])


def _orientation_lines(lines):
    out = []
    for m in lines:
        d = np.gradient(m, axis=0) if len(m) > 1 else np.array([[1.0, 0.0]])
        t = np.arctan2(d[:, 1], d[:, 0])
        out.append(np.column_stack([np.cos(2 * t), np.sin(2 * t)]))
    return np.concatenate(out)


def register(medians, outlines, ink, canvas, schedule=((3, 10), (5, 10), (7, 10)),
             reg=5e-5, max_d=120.0, alpha=40.0, centerline=None):
    """Fit a Warp taking reference medians (and outlines) onto the target ink.

    centerline: target points to match against (em frame); by default the
    skeleton of the ink. Bitmap glyphs pass their pixel centres instead, whose
    staircase skeleton would mislead the stroke directions.
    """
    allpts = np.concatenate(outlines)
    warp = Warp(fit_box((allpts.min(0), allpts.max(0)), ink_box(ink, canvas)))
    lines = [warp(resample(m)) for m in medians]
    P0 = np.concatenate(lines)
    sizes = np.cumsum([len(m) for m in lines])[:-1]
    if centerline is None:
        ys, xs = np.nonzero(skeletonize(ink))
        skel = canvas.pixel_centers(ys, xs)
    else:
        skel = np.asarray(centerline, float)
    if len(skel) < 2:
        return warp
    skel_f = np.column_stack([skel, alpha * _orientation_pca(skel)])
    tree = cKDTree(skel_f)
    offset = np.zeros_like(P0)
    for grid, iters in schedule:
        field = RBFField(grid)
        base = P0 + offset
        B = field.basis(base)
        n = len(field.centers)
        for _ in range(iters):
            P = base + B @ field.theta
            P_f = np.column_stack([P, alpha * _orientation_lines(np.split(P, sizes))])
            d1, j1 = tree.query(P_f)
            d2, j2 = cKDTree(P_f).query(skel_f)
            w1 = (d1 < max_d) / max((d1 < max_d).sum(), 1)
            w2 = (d2 < max_d) / max((d2 < max_d).sum(), 1)
            rows = np.concatenate([B, B[j2]])
            target = np.concatenate([skel[j1] - base, skel - base[j2]])
            w = np.concatenate([w1, w2])[:, None]
            field.theta = np.linalg.solve(rows.T @ (rows * w) + reg * np.eye(n), (rows * w).T @ target)
        offset = offset + B @ field.theta
        warp.fields.append(field)
    return warp
