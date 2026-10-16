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


class Linear:
    """x -> M x + t."""

    def __init__(self, M, t):
        self.M, self.t = np.asarray(M, float), np.asarray(t, float)

    def __call__(self, pts):
        return np.asarray(pts, float) @ self.M.T + self.t


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
    """A first map (affine, or another Warp) followed by successive displacement fields."""

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
    if centerline is None:
        ys, xs = np.nonzero(skeletonize(ink))
        skel = canvas.pixel_centers(ys, xs)
    else:
        skel = np.asarray(centerline, float)
    if len(skel) < 2:
        return warp
    target = _Target(skel, alpha)
    warp.fields += _fields(lines, target, target, schedule, reg, max_d, alpha)
    return warp


def skeleton(ink, canvas):
    ys, xs = np.nonzero(skeletonize(ink))
    return canvas.pixel_centers(ys, xs), ys, xs


def register_parts(medians, groups, starts, skel, owned, fallbacks=None, schedule=((3, 10), (5, 10), (7, 10)),
                   reg=2e-3, max_d=120.0, alpha=40.0, stiffness=300.0):
    """One warp per part, refining an earlier registration.

    Each part (a group of stroke indices) starts from its own warp (starts:
    the global box fit for top-level parts, the parent's warp below that) and
    is first fitted as a whole by an affine map onto the skeleton it covered
    after the earlier pass (owned: one boolean array over the skeleton points
    per part), far-off matches trimmed, so a part keeps its shape; a gentler
    displacement field then follows. Medians are drawn to the nearest ink
    anywhere, but only the part's own ink pulls back on them, so one part's
    medians are not dragged across another's ink. A part with too little ink
    of its own keeps its fallback warp (default: its start).
    Returns one Warp per group.
    """
    fallbacks = fallbacks or starts
    everything = _Target(skel, alpha)
    out = []
    for idx, own, start, fallback in zip(groups, owned, starts, fallbacks):
        if own.sum() < 10:
            out.append(fallback)
            continue
        mine = _Target(skel[own], alpha)
        src = [start(resample(medians[i])) for i in idx]
        fit = _affine_icp(src, everything, mine, max_d, alpha, stiffness)
        lines = [fit(m) for m in src]
        out.append(Warp(start, [fit, *_fields(lines, everything, mine, schedule, reg, max_d, alpha)]))
    return out


class _Target:
    """Skeleton points with direction features, for matching."""

    def __init__(self, pts, alpha):
        self.pts = pts
        self.feat = np.column_stack([pts, alpha * _orientation_pca(pts)]) if len(pts) > 1 else np.zeros((len(pts), 4))
        self.tree = cKDTree(self.feat)


def _features(lines, alpha):
    P = np.concatenate(lines)
    return np.column_stack([P, alpha * _orientation_lines(lines)])


def _pairs(lines, near, pull, max_d, alpha):
    """Matches median -> nearest target point, and pull point -> nearest median point.
    Returns (median point index, target position, weight) for both directions."""
    P_f = _features(lines, alpha)
    d1, j1 = near.tree.query(P_f)
    d2, j2 = cKDTree(P_f).query(pull.feat)
    w1 = (d1 < max_d) / max((d1 < max_d).sum(), 1)
    w2 = (d2 < max_d) / max((d2 < max_d).sum(), 1)
    return (np.concatenate([np.arange(len(P_f)), j2]), np.concatenate([near.pts[j1], pull.pts]),
            np.concatenate([w1, w2]))


def _affine_icp(src, near, pull, max_d, alpha, stiffness, iters=15):
    """Affine map of the median set onto the target, kept near identity by `stiffness`."""
    S = np.concatenate(src)
    M, t = np.eye(2), np.zeros(2)
    for it in range(iters):
        lines = [m @ M.T + t for m in src]
        # wide capture first, so a part can reach ink well off its first guess
        reach = max_d * (2.5 - 1.5 * min(it / (iters * 0.6), 1.0))
        i, q, w = _pairs(lines, near, pull, reach, alpha)
        X = np.column_stack([S[i], np.ones(len(i))])
        W = w[:, None]
        # minimise sum w |[M t] x - q|^2 + stiffness * |M - I|^2 / scale^2
        lam = stiffness / max(np.ptp(S, axis=0).max(), 1.0) ** 2
        A = X.T @ (X * W) + lam * np.diag([1.0, 1.0, 0.0])
        B = X.T @ (q * W) + lam * np.vstack([np.eye(2), np.zeros((1, 2))])
        sol = np.linalg.solve(A + 1e-9 * np.eye(3), B)
        M, t = sol[:2].T, sol[2]
    return Linear(M, t)


def _fields(lines, near, pull, schedule, reg, max_d, alpha):
    """Coarse-to-fine displacement fields moving the medians onto the target."""
    P0 = np.concatenate(lines)
    sizes = np.cumsum([len(m) for m in lines])[:-1]
    offset = np.zeros_like(P0)
    fields = []
    for grid, iters in schedule:
        field = RBFField(grid)
        base = P0 + offset
        B = field.basis(base)
        n = len(field.centers)
        for _ in range(iters):
            P = base + B @ field.theta
            i, q, w = _pairs(np.split(P, sizes), near, pull, max_d, alpha)
            rows, W = B[i], w[:, None]
            field.theta = np.linalg.solve(rows.T @ (rows * W) + reg * np.eye(n), (rows * W).T @ (q - base[i]))
        offset = offset + B @ field.theta
        fields.append(field)
    return fields
