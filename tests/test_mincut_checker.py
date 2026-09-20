"""Finite regression evidence for the analysis checker, not the mincut theorem."""

from collections import Counter
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.check_mincut_half_step import exchange_feasible, find_partition, is_tree, run


class MincutCheckerTests(unittest.TestCase):
    def test_cyclic_replacement_is_repaired_and_default_corpus_is_exhaustive(self):
        # Parents encode arcs parent[v] -> v. The replacement's a<->b cycle
        # cannot reach r, but averaging with the root star supplies two trees.
        star = (None, 0, 0)
        cycle = (None, 2, 1)
        self.assertFalse(is_tree(cycle))
        self.assertTrue(exchange_feasible(star, cycle))
        partition = find_partition(star, cycle)
        self.assertIsNotNone(partition)
        self.assertTrue(all(is_tree(tree) for tree in partition))

        def arc_copies(trees):
            return Counter((tree[v], v) for tree in trees for v in range(1, len(tree)))

        # Finding two trees is insufficient unless every original copy is used.
        self.assertEqual(arc_copies(partition), arc_copies((star, cycle)))

        # An arbitrary one-parent replacement does not satisfy the exchange
        # invariant: this union enters {a,b} only once and has no partition.
        chain = (None, 0, 1)
        self.assertFalse(exchange_feasible(chain, cycle))
        self.assertIsNone(find_partition(chain, cycle))

        corpus = run()  # The default covers n=2,3,4, not only the easy witness.
        self.assertEqual(
            [(row['n'], row['trees'], row['replacements'], row['pairs_checked'],
              row['feasible_pairs'], row['cyclic_feasible_pairs'],
              row['cut_checks'], row['partitions_found'])
             for row in corpus['results']],
            [(2, 1, 1, 1, 1, 0, 1, 1),
             (3, 3, 4, 12, 10, 1, 30, 10),
             (4, 16, 27, 432, 324, 68, 2268, 324)],
        )


if __name__ == '__main__':
    unittest.main()
