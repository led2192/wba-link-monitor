"""wikidata_count.py: the query and the counts, offline."""
import collections, unittest

import wikidata_count as W

A, B = "529900ABCDEFGHIJKL12", "549300ZZZZZZZZZZZZ34"


def v(x):
    return {"value": x}


class Count(unittest.TestCase):
    def test_query_takes_only_leis(self):
        q = W.query_for([A, 'x" } DROP', B])
        self.assertIn(f'VALUES ?lei {{ "{A}" "{B}" }}', q); self.assertNotIn("DROP", q)

    def test_tally(self):
        c = collections.Counter()
        answer = {"results": {"bindings": [{"lei": v(A), "site_": v("https://x.example"), "label_": v("Zebra"), "aliases": v("3"), "emp_": v("10")},
                                           {"lei": v(B), "aliases": v("0"), "rev_": v("5")}, {"lei": v(A), "site_": v("dup")}]}}
        self.assertEqual(W.tally(answer, c), {A, B})
        self.assertEqual((c["in Wikidata"], c["with a website"], c["with a name in English"], c["with other names in English"], c["with employees"], c["with revenue"], c["with a size (employees or revenue)"], c["with a stock exchange"]),
                         (2, 1, 1, 1, 1, 1, 2, 0))
        self.assertEqual(W.tally({}, c), set())

    def test_by_isin_an_entity_counts_once(self):
        q = W.query_for(["ES0000000001", "bad", "FR0000000002"], by="isin")
        self.assertIn("wdt:P946", q); self.assertNotIn("wdt:P1278", q); self.assertIn('"ES0000000001" "FR0000000002"', q); self.assertNotIn("bad", q)
        c, seen = collections.Counter(), set()
        answer = {"results": {"bindings": [{"lei": v("ES0000000001"), "site_": v("https://x.example")}, {"lei": v("FR0000000002"), "site_": v("https://x.example")}, {"lei": v("XX0000000003")}]}}
        W.tally(answer, c, {"ES0000000001": A, "FR0000000002": A}, seen)          # two ISINs of one entity; a third nobody asked for
        self.assertEqual((seen, c["in Wikidata"], c["with a website"]), ({A}, 1, 1))


if __name__ == "__main__":
    unittest.main()
