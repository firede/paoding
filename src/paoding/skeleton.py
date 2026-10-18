"""The target's own stroke structure: the skeleton of its ink as a graph.

Joints are where strokes meet: a skeleton branch point, its cluster of pixels
taken as one. Segments run between joints, or from a joint to a free end; a
closed ring with no joint (a lone 口) is one segment. Thinning leaves short
spurs at stroke ends and serifs and splits a crossing of thick strokes into
two branch points, so spurs shorter than the stroke they leave are pruned and
joints closer than their stroke widths are merged.

Everything is in the em frame; widths are half widths (the distance from the
skeleton to the outline).
"""

from dataclasses import dataclass

import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree
from skimage.morphology import skeletonize

_NB = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


@dataclass
class Joint:
    point: np.ndarray        # (2,) em frame
    radius: float            # half width of the ink there: the largest disc inside it
    degree: int              # segments meeting here


@dataclass
class Segment:
    points: np.ndarray       # (n, 2) em frame, in order along the skeleton
    ends: tuple              # joint index at each end, or -1 for a free end
    radius: np.ndarray       # (n,) half width at each point
    closed: bool = False
    bends: tuple = ()        # indices of points where it turns sharply: strokes meeting at a corner
    waists: tuple = ()       # indices of points where the ink narrows sharply: strokes touching end to end

    @property
    def length(self):
        return float(np.linalg.norm(np.diff(self.points, axis=0), axis=1).sum())


@dataclass
class Graph:
    joints: list
    segments: list


def graph(ink, canvas, spur=2.0, merge=1.0, bend=50.0, waist=0.65):
    """Skeleton graph of a boolean ink mask on canvas.

    spur: a branch from a joint to a free end is pruned when it is shorter than
    spur times the joint's radius (it ends inside the stroke it leaves, or just past it).
    merge: two joints are one when the segment between them is shorter than
    merge times the sum of their radii.
    bend: a turn sharper than this many degrees, over a stretch about as long as
    the stroke is wide, is a corner where one stroke ends and the next begins.
    waist: where the ink narrows to under this share of its width on both sides,
    two strokes touch end to end (a dot against a stroke, a 提 running into a 横).
    """
    sk = skeletonize(ink)
    dist = ndimage.distance_transform_edt(ink) / canvas.px
    ys, xs = np.nonzero(sk)
    pix = {(int(r), int(c)) for r, c in zip(ys, xs)}
    nbrs = {p: [(p[0] + dr, p[1] + dc) for dr, dc in _NB if (p[0] + dr, p[1] + dc) in pix] for p in pix}

    # clusters of branch pixels are joints; free ends are nodes too, without a joint
    branch = {p for p in pix if len(nbrs[p]) >= 3}
    node_of = {}
    clusters = []
    for p in sorted(branch):
        if p in node_of:
            continue
        stack, members = [p], []
        node_of[p] = len(clusters)
        while stack:
            q = stack.pop()
            members.append(q)
            for n in nbrs[q]:
                if n in branch and n not in node_of:
                    node_of[n] = len(clusters)
                    stack.append(n)
        clusters.append(members)
    ends = sorted(p for p in pix if len(nbrs[p]) <= 1)
    for p in ends:
        node_of[p] = len(clusters)
        clusters.append([p])
    is_joint = [len(m) and len(nbrs[m[0]]) >= 3 for m in clusters]

    # walk from every node along each branch to the next node
    raw, seen = [], set()
    for k, members in enumerate(clusters):
        for p in members:
            for q in nbrs[p]:
                if node_of.get(q) == k or (p, q) in seen:
                    continue
                path, prev, cur = [p], p, q
                while cur not in node_of:
                    path.append(cur)
                    nxt = [n for n in nbrs[cur] if n != prev]
                    if not nxt:
                        break
                    prev, cur = cur, nxt[0]
                path.append(cur)
                seen.add((p, q))
                seen.add((cur, path[-2]))
                if cur in node_of:
                    raw.append([k, node_of[cur], path])
    # rings without any node
    walked = {q for _, _, path in raw for q in path}
    for p in sorted(pix):
        if p in walked or p in node_of:
            continue
        path, prev, cur = [p], None, p
        walked.add(p)
        while True:
            nxt = [n for n in nbrs[cur] if n != prev]
            if not nxt or nxt[0] in walked:
                break
            prev, cur = cur, nxt[0]
            path.append(cur)
            walked.add(cur)
        if len(path) > 2:
            raw.append([-1, -1, path + [p]])

    def em(path):
        a = np.array(path)
        return canvas.pixel_centers(a[:, 0], a[:, 1]), dist[a[:, 0], a[:, 1]]

    joints = {}
    for k, members in enumerate(clusters):
        if is_joint[k]:
            a = np.array(members)
            pts = canvas.pixel_centers(a[:, 0], a[:, 1])
            joints[k] = [pts.mean(axis=0), float(dist[a[:, 0], a[:, 1]].max())]
    segs = []
    for a, b, path in raw:
        if a == b and a >= 0 and len(path) <= 2:
            continue
        pts, rad = em(path)
        segs.append([a if a in joints else -1, b if b in joints else -1, pts, rad, a == -1 and b == -1])

    # prune spurs and merge close joints until nothing changes
    changed = True
    while changed:
        changed = False
        degree = _degrees(segs)
        for s in segs:
            a, b, pts = s[0], s[1], s[2]
            if (a >= 0) != (b >= 0) and degree[max(a, b)] >= 3:
                j = max(a, b)
                if _length(pts) < spur * joints[j][1]:
                    segs = [x for x in segs if x is not s]
                    changed = True
                    break
        if changed:
            continue
        for s in segs:
            a, b, pts = s[0], s[1], s[2]
            if a >= 0 and b >= 0 and a != b and _length(pts) < merge * (joints[a][1] + joints[b][1]):
                joints[a] = [(joints[a][0] + joints[b][0]) / 2, max(joints[a][1], joints[b][1])]
                segs = [x for x in segs if x is not s]
                for t in segs:
                    t[0] = a if t[0] == b else t[0]
                    t[1] = a if t[1] == b else t[1]
                del joints[b]
                changed = True
                break
        if changed:
            continue
        # a joint left with fewer than three segments is no joint
        degree = _degrees(segs)
        for j in list(joints):
            touching = [s for s in segs if j in (s[0], s[1])]
            if degree.get(j, 0) == 2 and len(touching) == 2:
                s, t = touching
                segs = [x for x in segs if x is not t]
                s[:4] = _join(s, t, j)
            elif degree.get(j, 0) == 2:     # a ring through this joint alone
                touching[0][:2] = [-1, -1]
                touching[0][4] = True
            elif degree.get(j, 0) == 1:     # a free end now
                s = touching[0]
                s[0 if s[0] == j else 1] = -1
            elif degree.get(j, 0) >= 3:
                continue
            del joints[j]
            changed = True
            break

    order = sorted(joints)
    index = {j: i for i, j in enumerate(order)}
    degree = _degrees(segs)
    return Graph(
        [Joint(joints[j][0], joints[j][1], degree.get(j, 0)) for j in order],
        [Segment(pts, (index.get(a, -1), index.get(b, -1)), rad, closed, _bends(pts, rad, bend),
                 _waists(pts, rad, waist)) for a, b, pts, rad, closed in segs],
    )


def _bends(pts, rad, limit):
    """Indices where the skeleton turns by more than limit degrees, looking about
    1.5 half widths back and ahead; one index per turn, at its sharpest."""
    if len(pts) < 5:
        return ()
    s = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(pts, axis=0), axis=1))]
    reach = np.maximum(1.5 * rad, 12.0)
    back = np.searchsorted(s, s - reach)
    ahead = np.minimum(np.searchsorted(s, s + reach), len(pts) - 1)
    u, v = pts - pts[back], pts[ahead] - pts
    nu, nv = np.linalg.norm(u, axis=1), np.linalg.norm(v, axis=1)
    ok = (nu > 0.5 * reach) & (nv > 0.5 * reach)    # a full look both ways: not at an end
    cos = np.einsum("ij,ij->i", u, v) / np.maximum(nu * nv, 1e-9)
    turn = np.where(ok, np.degrees(np.arccos(np.clip(cos, -1, 1))), 0.0)
    out, i = [], 0
    while i < len(turn):
        if turn[i] <= limit:
            i += 1
            continue
        j = i
        while j < len(turn) and turn[j] > limit:
            j += 1
        out.append(int(i + np.argmax(turn[i:j])))
        i = j
    return tuple(out)


def _degrees(segs):
    degree = {}
    for s in segs:
        for j in (s[0], s[1]):
            if j >= 0:
                degree[j] = degree.get(j, 0) + 1
    return degree


def _length(pts):
    return float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum())


def _join(s, t, j):
    """Two segments meeting at joint j -> one, as [a, b, points, radius]."""
    def facing(seg, out):     # oriented so the joint is at the end (out) or the start
        a, b, pts, rad = seg[:4]
        if (b == j) == out:
            return a, b, pts, rad
        return b, a, pts[::-1], rad[::-1]
    a, _, p1, r1 = facing(s, True)
    _, b, p2, r2 = facing(t, False)
    return [a, b, np.concatenate([p1, p2[1:]]), np.concatenate([r1, r2[1:]])]


def _waists(pts, rad, ratio, reach=60.0, gap=8.0):
    """Indices where the half width drops under ratio of its median over reach
    units on either side (leaving gap units out); one index per narrowing, at its
    narrowest. Points with less than most of reach on either side (near a free
    end, where strokes taper) are not considered."""
    if len(pts) < 5:
        return ()
    s = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(pts, axis=0), axis=1))]
    narrow = np.zeros(len(pts), bool)
    for i in range(len(pts)):
        if s[i] < 0.8 * reach or s[-1] - s[i] < 0.8 * reach:
            continue
        before = rad[(s >= s[i] - reach) & (s < s[i] - gap)]
        after = rad[(s > s[i] + gap) & (s <= s[i] + reach)]
        if len(before) >= 3 and len(after) >= 3:
            narrow[i] = rad[i] < ratio * min(np.median(before), np.median(after))
    out, i = [], 0
    while i < len(pts):
        if not narrow[i]:
            i += 1
            continue
        j = i
        while j < len(pts) and narrow[j]:
            j += 1
        out.append(int(i + np.argmin(rad[i:j])))
        i = j
    return tuple(out)


def stretches(g, margin=1.25):
    """The parts of each segment away from its joints and bends, as (segment, start, stop).

    Within margin times a joint's (or bend's, or waist's) radius of it, strokes meet and the
    ink could belong to either; elsewhere a stretch is one stroke, or strokes
    running on along one line, and should have one owner.
    """
    out = []
    for k, s in enumerate(g.segments):
        keep = np.ones(len(s.points), bool)
        for j in s.ends:
            if j >= 0:
                jt = g.joints[j]
                keep &= np.linalg.norm(s.points - jt.point, axis=1) > margin * jt.radius
        for b in s.bends:
            keep &= np.linalg.norm(s.points - s.points[b], axis=1) > margin * s.radius[b]
        for w in s.waists:     # the narrowest point says little about the strokes' width
            near = np.abs(np.arange(len(s.points)) - w) <= 20
            keep &= np.linalg.norm(s.points - s.points[w], axis=1) > margin * np.median(s.radius[near])
        edges = np.flatnonzero(np.diff(np.r_[0, keep.astype(int), 0]))
        out += [(k, int(a), int(b)) for a, b in zip(edges[::2], edges[1::2]) if b - a >= 2]
    return out


def _steps(pts):
    d = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    return np.r_[d, 0.0] / 2 + np.r_[0.0, d] / 2


def _owners(pts, labels, canvas):
    r, c = np.round(canvas.index(pts)).astype(int)
    return labels[np.clip(r, 0, canvas.res - 1), np.clip(c, 0, canvas.res - 1)]


def owned(g, spans, labels, canvas):
    """Per stretch: {owner index: length of the stretch on that owner's ink}, from a raster of owners (-1: none)."""
    out = []
    for k, a, b in spans:
        pts = g.segments[k].points[a:b]
        own = _owners(pts, labels, canvas)
        lengths = {}
        for o, d in zip(own, _steps(pts)):
            if o >= 0:
                lengths[int(o)] = lengths.get(int(o), 0.0) + float(d)
        out.append(lengths)
    return out


def voted(g, spans, lines, owner, skip=None, reach=15.0, angle=40.0):
    """Per stretch: {owner index: length of registered medians running along it}.

    lines: medians (em frame); owner[i]: the owner index of line i; skip[i]:
    boolean mask of points of line i that do not count. A median point votes
    for the nearest stretch point if it lies within that point's half width plus
    reach and runs within angle degrees of the skeleton's direction there.
    """
    out = [{} for _ in spans]
    if not spans:
        return out
    pts, rad, tan, which = [], [], [], []
    for n, (k, a, b) in enumerate(spans):
        s = g.segments[k]
        p = s.points
        lo, hi = np.maximum(np.arange(a, b) - 3, 0), np.minimum(np.arange(a, b) + 3, len(p) - 1)
        t = p[hi] - p[lo]
        pts.append(p[a:b]), rad.append(s.radius[a:b]), which.append(np.full(b - a, n))
        tan.append(t / np.maximum(np.linalg.norm(t, axis=1, keepdims=True), 1e-9))
    pts, rad, tan, which = map(np.concatenate, (pts, rad, tan, which))
    tree = cKDTree(pts)
    cos = np.cos(np.radians(angle))
    for i, m in enumerate(lines):
        if len(m) < 2:
            continue
        t = np.gradient(m, axis=0)
        t /= np.maximum(np.linalg.norm(t, axis=1, keepdims=True), 1e-9)
        d, j = tree.query(m)
        ok = (d <= rad[j] + reach) & (np.abs(np.einsum("ij,ij->i", t, tan[j])) >= cos)
        if skip is not None and skip[i] is not None:
            ok &= ~skip[i]
        for n, w in zip(which[j[ok]], _steps(m)[ok]):
            out[n][owner[i]] = out[n].get(owner[i], 0.0) + float(w)
    return out


def disputes(g, spans, own, votes, labels, canvas, min_len=None, lead=2.0, cover=0.5, min_vote=2.0):
    """Stretches whose ink is split between owners, or given to an owner the medians do not run along.

    split: a second owner holds at least min_len of it (default: the stroke's half
    width, at least 10 units). vote: the medians along it are mostly (lead times
    more) one owner's, covering at least cover of its length, and that is not the
    owner of most of its ink; only on stretches at least min_vote half widths
    long (shorter ones, stubs past a joint, draw stray votes).
    The point given is the middle of the longest run of a minority owner for a
    split (where the wrong piece is), the middle of the stretch for a vote.
    """
    out = []
    for (k, a, b), lengths, vote in zip(spans, own, votes):
        s = g.segments[k]
        if not lengths:
            continue
        floor = min_len if min_len is not None else max(float(np.median(s.radius[a:b])), 10.0)
        major = max(lengths, key=lengths.get)
        minor = sum(v for o, v in lengths.items() if o != major)
        total = sum(_steps(s.points[a:b]))
        winner = max(vote, key=vote.get) if vote else None
        kind = None
        if minor >= floor:
            kind = "split"
        elif (winner is not None and winner != major and total >= max(min_vote * floor, 30.0)
              and vote[winner] >= cover * total
              and vote[winner] >= lead * vote.get(major, 0.0)):
            kind = "vote"
        at = s.points[(a + b) // 2]
        if kind == "split":
            at = _minority(s.points[a:b], _owners(s.points[a:b], labels, canvas), major)
        if kind:
            out.append({"kind": kind, "segment": k, "span": (a, b), "at": at,
                        "ink": dict(sorted(lengths.items(), key=lambda kv: -kv[1])),
                        "vote": dict(sorted(vote.items(), key=lambda kv: -kv[1])), "length": total})
    return out


def _minority(pts, own, major):
    """The middle point of the longest run of points owned by someone other than major."""
    steps = _steps(pts)
    best, run, start = (0.0, 0, 0), 0.0, None
    for i, o in enumerate(np.r_[own, major]):
        if o >= 0 and o != major:
            if start is None:
                start, run = i, 0.0
            run += steps[i]
        elif start is not None:
            best = max(best, (run, start, i))
            start = None
    _, a, b = best
    return pts[(a + b - 1) // 2] if b > a else pts[len(pts) // 2]


def strays(masks, labels, ink, canvas, share=0.15, floor=400.0):
    """Islands of an owner's overlay ink inside ink that the flat view gives to others.

    masks: owner index -> boolean raster of its overlay ink (connected through
    shared faces where its strokes cross others). An island of it that is not a
    whole piece of ink by itself, holds under share of the owner's ink and under
    share of the piece of ink it lies in, and is mostly another's in the flat
    view (labels), is the overlay reaching into someone else's stroke with
    nothing of the owner's own around it. (An island the flat view gives to the
    owner too is usually a whole stroke touching another part's: a dot, a 横.)
    floor: the smallest island counted, in square units. Returns dicts: owner
    index, a point in the island, its area and the owner's whole area.
    """
    pieces, _ = ndimage.label(ink, structure=np.ones((3, 3)))
    sizes = np.bincount(pieces.ravel())
    unit = 1.0 / canvas.px ** 2
    out = []
    for o, m in masks.items():
        total = m.sum()
        islands, n = ndimage.label(m, structure=np.ones((3, 3)))
        for i in range(1, n + 1):
            island = islands == i
            area = island.sum()
            p = np.bincount(pieces[island]).argmax()
            if area * unit < floor or area >= share * total or area >= share * sizes[p]:
                continue
            if (labels[island] == o).mean() >= 0.5:
                continue
            ys, xs = np.nonzero(island)
            k = np.argmin((ys - ys.mean()) ** 2 + (xs - xs.mean()) ** 2)
            out.append({"owner": o, "at": canvas.pixel_centers(ys[k:k + 1], xs[k:k + 1])[0],
                        "area": float(area * unit), "total": float(total * unit)})
    return out
