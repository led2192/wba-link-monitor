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


if __name__ == "__main__":
    unittest.main()
