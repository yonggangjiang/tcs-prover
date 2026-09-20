#!/usr/bin/env python3
"""Finite check of the supplied PDF's integral exchange/half-step invariant.

Exhausts all rooted out-trees T and one-incoming-arc selections B on complete
loopless digraphs with no arcs entering root 0, for n=2,...,--max-n. Feasibility is
checked by an independent bipartite matching of added arcs to removed tree
arcs on their fundamental paths. Checks every cut and finds a partition of
T+B into two arborescences by exhaustive choices of incoming arc copies.
This is an exhaustive finite sanity check, not a mathematical proof, a test of
fractional rounding, or a production almost-linear mincut solver. In particular,
it uses exponential enumeration and only tests simple graphs, not parallel arc
identities. Passing cannot establish the general theorem or its running time.
This analysis-only tool is not included in research prompts. Adapted from the
workspace's pre-existing output/analysis/check_half_step.py; stdlib only.

Example: python3 tools/check_mincut_half_step.py --max-n 5
"""

import argparse
from itertools import product
import json


def is_tree(parents):
    for start in range(1, len(parents)):
        seen = set()
        v = start
        while v:
            if v in seen:
                return False
            seen.add(v)
            v = parents[v]
    return True


def path_edges(parents, u, v):
    ancestors = set()
    x = u
    while x:
        ancestors.add(x)
        x = parents[x]
    ancestors.add(0)
    right = set()
    while v not in ancestors:
        right.add(v)
        v = parents[v]
    while u != v:
        right.add(u)
        u = parents[u]
    return right  # A tree arc is identified by its nonroot head.


def exchange_feasible(tree, replacement):
    changed = {v for v in range(1, len(tree)) if tree[v] != replacement[v]}
    choices = {v: path_edges(tree, replacement[v], v) & changed for v in changed}
    matched = {}

    def augment(v, seen):
        for e in sorted(choices[v]):
            if e in seen:
                continue
            seen.add(e)
            if e not in matched or augment(matched[e], seen):
                matched[e] = v
                return True
        return False

    return all(augment(v, set()) for v in sorted(changed))


def find_partition(tree, replacement):
    n = len(tree)
    for bits in product((0, 1), repeat=n - 1):
        left = (None,) + tuple((tree[v], replacement[v])[bits[v - 1]] for v in range(1, n))
        right = (None,) + tuple((replacement[v], tree[v])[bits[v - 1]] for v in range(1, n))
        if is_tree(left) and is_tree(right):
            return left, right
    return None


def run(max_n=4):
    if not 2 <= max_n <= 5:
        raise ValueError("max_n must be between 2 and 5 for this finite check")
    results = []
    first_cyclic = None
    for n in range(2, max_n + 1):
        replacements = [(None,) + p for p in product(*[
            [u for u in range(n) if u != v] for v in range(1, n)
        ])]
        trees = [p for p in replacements if is_tree(p)]
        stats = dict(n=n, trees=len(trees), replacements=len(replacements),
                     pairs_checked=0, feasible_pairs=0, cyclic_feasible_pairs=0,
                     cut_checks=0, partitions_found=0)
        for tree in trees:
            for replacement in replacements:
                stats['pairs_checked'] += 1
                if not exchange_feasible(tree, replacement):
                    continue
                stats['feasible_pairs'] += 1
                cyclic = not is_tree(replacement)
                stats['cyclic_feasible_pairs'] += cyclic
                for mask in range(1, 1 << (n - 1)):
                    side = {v for v in range(1, n) if mask & (1 << (v - 1))}
                    incoming = sum(parents[v] not in side for parents in (tree, replacement) for v in side)
                    if incoming < 2:
                        raise AssertionError((tree, replacement, side))
                    stats['cut_checks'] += 1
                partition = find_partition(tree, replacement)
                if partition is None:
                    raise AssertionError((tree, replacement))
                stats['partitions_found'] += 1
                if cyclic and first_cyclic is None:
                    first_cyclic = dict(tree=tree, cyclic_replacement=replacement, partition=partition)
        results.append(stats)
    return dict(scope=f'All simple complete rooted digraphs n=2..{max_n}; integral replacements only',
                results=results, first_cyclic_example=first_cyclic,
                conclusion='All checked feasible unions satisfy every rooted cut bound and decompose into two arborescences.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-n', type=int, choices=range(2, 6), default=4,
                        help='largest vertex count to enumerate (default: 4; maximum: 5)')
    args = parser.parse_args()
    print(json.dumps(run(args.max_n), indent=2))
