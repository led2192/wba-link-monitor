"""ask_concept_compare.py: the counts of a comparison, offline."""
import unittest

import ask_concept_compare as C


def arm(**kw):
    return dict({"pages": ["A|1"], "chars": 500, "cites": 3, "hedged": False, "not_stated": False, "marked": 0, "named": False}, **kw)


class Compare(unittest.TestCase):
    def test_subsidiary_counts(self):
        trials = [{"off": arm(marked=2), "on": arm(marked=2, named=True)}, {"off": arm(marked=1, named=True), "on": arm(marked=1, named=True)},
                  {"off": arm(named=True), "on": arm(named=True)}, {"skipped": "no page found"}]
        c = C.summarise(trials)
        self.assertEqual((c["compared"], c["marked"], c["named_off"], c["named_on"]), (3, 2, 1, 2))          # an answer that names it with no such page read is not counted

    def test_series_counts_and_questions(self):
        trials = [{"off": arm(years=2, page_years=2), "on": arm(years=6, page_years=7)}, {"off": arm(years=5, page_years=3), "on": arm(years=4, page_years=6)}]
        c = C.summarise(trials)
        self.assertEqual((c["years_off"], c["years_on"], c["more_years"], c["fewer_years"], c["page_years_on"]), (3.5, 5.0, 1, 1, 6.5))
        got = C.cases(3, ("A",), C.SERIES)
        self.assertEqual([w for w, _, _ in got], [C.SERIES[0][0], C.SERIES[1][0], C.SERIES[2][0]]); self.assertTrue(all(not y for _, _, y in got))

    def test_cases_take_the_companies_given(self):
        got = C.cases(9, ("A", "B"))
        self.assertEqual(len(got), 9)
        self.assertEqual({c for _, c, _ in got}, {"A", "B"})
        self.assertEqual(C.cases(1)[0][1], C.COMPANIES[0])


if __name__ == "__main__":
    unittest.main()
