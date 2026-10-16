"""Ideographic Description Sequences and the node paths that address their parts.

A node path is a tuple of child indices from the root: () is the whole
character, (0,) the first top-level part, (1, 0) the first part of the second.
As text it is written "0", "1.0"; the root is "".
"""

ARITY = {c: 2 for c in "⿰⿱⿴⿵⿶⿷⿸⿹⿺⿻"} | {"⿲": 3, "⿳": 3}
UNKNOWN = "？"


def parse(ids):
    """IDS string -> tree: a leaf is a str, an inner node is (operator, [children])."""
    pos = 0

    def node():
        nonlocal pos
        if pos >= len(ids):
            raise ValueError(f"truncated IDS: {ids!r}")
        ch = ids[pos]
        pos += 1
        if ch in ARITY:
            return (ch, [node() for _ in range(ARITY[ch])])
        return ch

    tree = node()
    if pos != len(ids):
        raise ValueError(f"trailing characters in IDS: {ids!r}")
    return tree


def unparse(tree):
    """Tree -> IDS string."""
    return tree if is_leaf(tree) else tree[0] + "".join(unparse(c) for c in tree[1])


def replace(tree, path, new):
    """Copy of tree with the node at path replaced by new."""
    if not path:
        return new
    op, children = tree
    children = list(children)
    children[path[0]] = replace(children[path[0]], path[1:], new)
    return (op, children)


def leaf_paths(tree, path=()):
    """(path, leaf) for every leaf."""
    if is_leaf(tree):
        return [(path, tree)]
    return [x for i, c in enumerate(tree[1]) for x in leaf_paths(c, path + (i,))]


def is_leaf(tree):
    return isinstance(tree, str)


def leaves(tree):
    return tree if is_leaf(tree) else "".join(leaves(c) for c in tree[1])


def subtree(tree, path):
    for i in path:
        tree = tree[1][i]
    return tree


def depth(tree):
    return 0 if is_leaf(tree) else 1 + max(depth(c) for c in tree[1])


def fmt(path):
    return ".".join(map(str, path))


def unfmt(text):
    return tuple(int(x) for x in text.split(".")) if text else ()
