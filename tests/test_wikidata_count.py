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


if __name__ == "__main__":
    unittest.main()
