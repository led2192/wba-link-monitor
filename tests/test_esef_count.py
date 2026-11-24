"""esef_count.py: the counts over the repository's pages, offline."""
import unittest

import esef_count as E

A, B = "529900ABCDEFGHIJKL12", "549300ZZZZZZZZZZZZ34"


def filing(fid, ent, country, end, url="/x/report.xhtml"):
    return {"type": "filing", "id": fid, "attributes": {"country": country, "period_end": end, "report_url": url}, "relationships": {"entity": {"data": {"type": "entity", "id": ent}}}}


class Count(unittest.TestCase):
    def test_counts(self):
        inc = [{"type": "entity", "id": "1", "attributes": {"identifier": A, "name": "x"}}, {"type": "entity", "id": "2", "attributes": {"identifier": B}}]
        pages = [{"data": [filing("f1", "1", "ES", "2024-12-31"), filing("f2", "1", "ES", "2023-12-31", None)], "included": inc},
                 {"data": [filing("f3", "2", "fr", "2024-12-31"), filing("f4", "9", "DE", "2024-12-31")], "included": inc}]
        c, by_country, by_year = E.count(pages, {A})
        self.assertEqual((c["filings"], c["with an address"], c["with no LEI found"], c["companies"], c["companies held by lantrNa"], c["companies not held"]), (4, 3, 1, 2, 1, 1))
        self.assertEqual((dict(by_country), dict(by_year)), ({"ES": 1, "FR": 1}, {2024: 2, 2023: 1}))

    def test_the_lei_in_the_filing_itself(self):
        item = {"attributes": {"entity_lei": A.lower(), "country": "NL"}}
        self.assertEqual(E.lei_of(item, {}), A)
        self.assertIsNone(E.lei_of({"attributes": {"name": A}}, {}))          # a field that is not an identifier is not read as one
        self.assertEqual(E.facts({"attributes": {"country": "nl", "period_end": "x"}}), ("NL", None, False))


if __name__ == "__main__":
    unittest.main()
