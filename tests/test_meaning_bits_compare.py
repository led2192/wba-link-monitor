"""meaning_bits_compare.py: the shares and the report; the small index's definition. No network."""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import meaning_bits_compare as M
import semantic_index as S


class Shares(unittest.TestCase):
    def test_overlap_counts_the_full_indexs_pages_the_bits_hold(self):
        full = [("a", 1), ("a", 2), ("b", 1), ("c", 9)]
        self.assertEqual(M.overlap(full, [("a", 2), ("a", 1), ("z", 1), ("c", 9)], 4), 0.75)
        self.assertEqual(M.overlap(full, [("a", 2), ("a", 1)], 2), 1.0)          # the order inside the first k does not count
        self.assertIsNone(M.overlap([], [("a", 1)], 10))

    def test_the_sums_leave_out_queries_the_full_index_found_nothing_for(self):
        rows = [([("a", 1), ("b", 1)], [("a", 1), ("c", 1)], 10.0, 30.0), ([], [], 5.0, 7.0), ([("x", 1)], [("x", 1)], 20.0, 50.0)]
        s = M.summarise(rows)
        self.assertEqual((s["queries"], s["with_pages"]), (3, 2))
        self.assertAlmostEqual(s["at10"], 0.75); self.assertEqual(s["first_same"], 1.0)
        self.assertEqual((s["ms_full"], s["ms_bits"], s["ms_bits_max"]), (10.0, 30.0, 50.0))
        text = M.report(s, 800)
        self.assertIn("| of the full index's first 10 pages, found by the bits | 75.0% |", text)
        self.assertNotIn("('a'", text)                                              # no page is printed

    def test_both_ways_use_their_index_expression(self):
        self.assertIn("(c.embedding::extensions.halfvec(384)) operator(extensions.<=>)", M.FULL_SQL)
        self.assertIn("(public.chunk_bits(c.embedding)) operator(extensions.<~>) public.chunk_bits(%s::real[]::extensions.vector(384))", M.BITS_SQL)
        self.assertIn("embedding operator(extensions.<=>) %s::real[]::extensions.vector(384) as dist from cand", M.BITS_SQL)      # ordered again by the whole embeddings


class Index(unittest.TestCase):
    def test_the_signs_index_stands_beside_the_full_one_and_is_never_dropped_for_a_fill(self):
        self.assertIn("create index if not exists chunks_signs on public.chunks using hnsw", S.BITS_INDEX_SQL)
        self.assertIn("((public.chunk_bits(embedding)) extensions.bit_hamming_ops)", S.BITS_INDEX_SQL)
        self.assertNotIn("chunks_signs", S.FILL_INDEXES)
        self.assertIn("operator(extensions.-) '{mean}'::extensions.vector(384)", S.BITS_FUNCTION_SQL)      # the signs of the embedding minus the mean

    class Cur:
        def __init__(self, function_there): self.sql = []; self.connection = self; self.there = function_there
        def execute(self, q, *a): self.sql.append(q)
        def fetchone(self):
            q = self.sql[-1]
            return (self.there,) if "to_regprocedure" in q else ("[0.1,-0.25,3e-05]", 82000) if "avg(embedding)" in q else (4000000,) if "count(*)" in q else ("1300 MB",)
        def commit(self): self.sql.append("commit")

    def test_the_mean_is_written_once_and_never_replaced(self):
        cur, said = self.Cur(False), []
        self.assertTrue(S.ensure_bits_function(cur, say=lambda *a, **k: said.append(a[0])))
        made = [q for q in cur.sql if q.startswith("create function public.chunk_bits")]
        self.assertEqual(len(made), 1); self.assertIn("'[0.1,-0.25,3e-05]'::extensions.vector(384)", made[0])
        self.assertNotIn("or replace", made[0])
        cur = self.Cur(True)
        self.assertFalse(S.ensure_bits_function(cur))
        self.assertFalse(any("create function" in q for q in cur.sql))                  # an index built on it would be wrong for another mean

    def test_building_it_drops_only_the_first_useless_index(self):
        cur, said = self.Cur(True), []
        S.build_bits_index(cur, "1GB", 2, say=lambda *a, **k: said.append(a[0]))
        self.assertIn("set maintenance_work_mem = '1GB'", cur.sql); self.assertIn(S.BITS_INDEX_SQL, cur.sql)
        self.assertEqual([q for q in cur.sql if "drop index" in q], ["drop index if exists public.chunks_bits"])
        self.assertTrue(any("1300 MB" in x for x in said))


if __name__ == "__main__":
    unittest.main()
