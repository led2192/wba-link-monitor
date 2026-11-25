"""catalogue_size.py: the newest revenue of a report, the rough euros for ordering, and what is said of a company, offline."""
import unittest

import catalogue_size as C


class Size(unittest.TestCase):
    def test_the_newest_year_and_the_preferred_concept(self):
        facts = [(1, "ifrs-full:RevenueFromContractsWithCustomers", 2025, "2025-12-31", 90.0, "SEK", "ESEF", ""),
                 (0, "ifrs-full:Revenue", 2025, "2025-12-31", 100.0, "SEK", "ESEF", ""),
                 (0, "ifrs-full:Revenue", 2024, "2024-12-31", 500.0, "SEK", "ESEF", "")]
        self.assertEqual(C.newest(facts), (100.0, "SEK", 2025))
        self.assertIsNone(C.newest([])); self.assertIsNone(C.newest([(0, "ifrs-full:Revenue", 2025, "2025-12-31", 0.0, "EUR", "ESEF", "")]))

    def test_euros_are_rough_and_only_for_known_currencies(self):
        self.assertEqual(C.in_euros(1000.0, "eur"), 1000); self.assertEqual(C.in_euros(1000.0, "SEK"), 90); self.assertIsNone(C.in_euros(5.0, "XYZ"))

    def test_what_is_said_of_a_company(self):
        year = "2025-01-01T00:00:00/2026-01-01T00:00:00"
        oim = {"facts": {"f1": {"value": "2500000", "dimensions": {"concept": "ifrs-full:Revenue", "entity": "x", "period": year, "unit": "iso4217:EUR"}},
                         "f2": {"value": "9", "dimensions": {"concept": "ifrs-full:Revenue", "entity": "x", "period": year, "unit": "iso4217:EUR", "ifrs-full:SegmentsAxis": "a"}}}}
        fl = lambda lei, sess, years: [("2024-12-31", "old.json", "", ""), ("2025-12-31", "new.json", "", "")]
        asked = []
        ok = lambda url: (asked.append(url), (200, oim))[1]
        self.assertEqual(C.size_of("L", None, fl, ok), ("ok", (2500000.0, "EUR", 2025))); self.assertEqual(asked, ["new.json"])      # the newest filing only; a segment is not the group
        self.assertEqual(C.size_of("L", None, lambda *a: [], ok), ("no filing", None))
        self.assertEqual(C.size_of("L", None, fl, lambda url: (429, {})), ("refused", None))
        self.assertEqual(C.size_of("L", None, fl, lambda url: (200, {})), ("no facts", None))
        bank = {"facts": {"b": {"value": "1", "dimensions": {"concept": "ifrs-full:InterestIncome", "period": year, "unit": "iso4217:EUR"}}}}
        import collections
        why = collections.Counter()
        self.assertEqual(C.size_of("L", None, fl, lambda url: (200, bank), why=why), ("no revenue", None))
        self.assertEqual(dict(why), {("ifrs-full:InterestIncome", "plain", "a year", "money"): 1})          # names of the taxonomy only


if __name__ == "__main__":
    unittest.main()
