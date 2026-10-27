import os
import unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")

import gold_eval as G


class Helpers(unittest.TestCase):
    def test_gold_id(self):
        self.assertEqual(G.gold_id("PT_01897-GMC_00064.1-2024"), ("PT_01897", "GMC_00064.1", 2024))
        self.assertEqual(G.gold_id("junk"), (None, None, None))

    def test_gold_pages(self):
        self.assertEqual(G.gold_pages("63"), [63])
        self.assertEqual(G.gold_pages("p. 63-64"), [63, 64])
        self.assertEqual(G.gold_pages("12, 14 and 160"), [12, 14, 160])
        self.assertEqual(G.gold_pages("p. 7 and 9"), [7, 9])
        self.assertEqual(G.gold_pages(""), [])

    def test_quote_phrases(self):
        q = ("AGL defines climate solutions as grid-scale renewable generation, grid-scale batteries and pumped hydro.\n\n"
             "7.2.3 Capital allocation to support the energy transition\nAGL has and will continue to evolve our capital allocation.")
        ph = G.quote_phrases(q)
        self.assertEqual(ph[0], "AGL defines climate solutions as grid-scale")
        self.assertTrue(any(p.startswith("Capital allocation to support") or p.startswith("7.2.3") or "Capital allocation" in p for p in ph[1:]))
        self.assertEqual(G.quote_phrases("short"), [])

    def test_or_query(self):
        self.assertEqual(G.or_query('"scope 1" emissions tCO2e'), '"scope 1" or emissions or tCO2e')
        self.assertEqual(G.or_query("total revenue"), "total or revenue")

    def test_locate(self):
        # a specific phrase: its pages
        self.assertEqual(G.locate([[], [63]], [60]), {63})
        # a running header on every page: fall back to the analyst's page, with two pages of play
        self.assertEqual(G.locate([list(range(1, 90))], [63]), {61, 62, 63, 64, 65})
        self.assertEqual(G.locate([list(range(1, 90))], []), set())
        # nothing found, analyst page known
        self.assertEqual(G.locate([[], []], [7]), {5, 6, 7, 8, 9})

    def test_rank_of(self):
        self.assertEqual(G.rank_of({63}, [5, 63, 70]), 2)
        self.assertEqual(G.rank_of({63, 5}, [5, 63, 70]), 1)
        self.assertIsNone(G.rank_of({99}, [5, 63, 70]))

    def test_summarize(self):
        s = G.summarize([{"attribute": "A", "source_found": True, "in_db": True, "quote_found": True, "best_rank": 2},
                         {"attribute": "A", "source_found": False}, {"attribute": "A", "source_found": True, "in_db": True, "best_rank": 7}])
        self.assertEqual(s["A"]["answers"], 3)
        self.assertEqual(s["A"]["source found"], 2)
        self.assertEqual((s["A"]["top 1"], s["A"]["top 3"], s["A"]["top 10"]), (0, 1, 2))


class DocScores(unittest.TestCase):
    """doc_scores asks one statement per document; the SQL is built from the counts of phrases and
    queries, and the Python side turns the rows into the same shapes pages_with_phrase and ranking gave."""

    class Cur:
        def __init__(self, rows): self.rows, self.sql, self.params = rows, None, None
        def execute(self, sql, params): self.sql, self.params = sql, params
        def fetchall(self): return self.rows

    def test_shapes(self):
        # page, phrase 0, phrase 1, score of query 0, score of query 1
        cur = self.Cur([(1, True, False, 0.5, None), (2, False, True, 0.9, 0.1), (3, False, False, None, 0.2)])
        n, found, ranks = G.doc_scores(cur, "doc", ["net zero", "water"], ["net or zero", "water"])
        self.assertEqual(n, 3)
        self.assertEqual(found, [[1], [2]])
        self.assertEqual(ranks, [[2, 1], [3, 2]])                 # best score first, page breaks ties
        self.assertEqual(cur.params, ['"net zero"', '"water"', "net or zero", "water", "doc"])
        self.assertIn("p.library_id = %s", cur.sql)
        self.assertEqual(cur.sql.count("websearch_to_tsquery"), 4)

    def test_no_phrases_or_queries(self):
        cur = self.Cur([(1,), (2,)])
        self.assertEqual(G.doc_scores(cur, "doc", [], []), (2, [], []))
        self.assertIn("1 as none", cur.sql)

    @unittest.skipUnless(os.environ.get("PG_TEST_URL"), "set PG_TEST_URL to a throwaway Postgres (after tests/test_load_supabase.py)")
    def test_against_postgres(self):
        import psycopg
        with psycopg.connect(os.environ["PG_TEST_URL"]) as c:
            cur = c.cursor()
            if not cur.execute("select count(*) from public.pages where library_id = 'old'").fetchone()[0]:
                self.skipTest("run tests/test_load_supabase.py first")
            self.assertEqual(G.doc_scores(cur, "old", ["net zero target", "water withdrawal"], [G.or_query("net zero"), G.or_query("water"), G.or_query("nothing here")]),
                             (2, [[1], [2]], [[1], [2], []]))
            self.assertEqual(G.doc_scores(cur, "missing", ["x"], ["y"]), (0, [[]], [[]]))


if __name__ == "__main__":
    unittest.main()
