"""Offline tests for label_mining.py: finding a printed value in its separators, the words before it, and
the ranking of phrases against ordinary pages."""
import collections, os, sys, unittest

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import label_mining as L  # noqa: E402
from value_eval import CURRENCY_SCALES, EMISSION_SCALES, printed_forms  # noqa: E402


class Mining(unittest.TestCase):
    def test_number_spans(self):
        self.assertEqual(len(L.number_spans("Revenue 102,502 and 102.502 and 102 502", 102502, 0)), 3)
        self.assertEqual(L.number_spans("Scope 1 7,50,616 tCO2e", 750616, 0), [8])
        self.assertEqual(L.number_spans("Revenue 1,102,502", 102502, 0), [])         # part of a larger number
        self.assertEqual(L.number_spans("x 30,142.0 y", 30142.0, 1), [2])
        self.assertEqual(L.number_spans("x 30,142.05", 30142.0, 1), [])

    def test_window_and_phrases(self):
        text = "Taxonomy-aligned CapEx (EUR million) 2024 1,234"
        w = L.label_window(text, text.index("1,234"))
        self.assertEqual(w, ["taxonomy-aligned", "capex", "eur", "million"])
        ph = L.phrases(w)
        self.assertIn("taxonomy-aligned capex", ph)
        self.assertNotIn("capex eur", ph)                                           # ends on a stop word

    def test_words_keep_scope_numbers_and_co2e(self):
        self.assertEqual(L.words("Scope 1 (tCO2e) Scope 2 market-based 2024 1,234 15% 3.5"),
                         ["scope", "1", "tco2e", "scope", "2", "market-based"])
        self.assertIn("scope 2", L.phrases(L.words("Total Scope 2 emissions")))

    def test_mine_answer(self):
        pages = [("Annual Report", "Consolidated income statement (in USD million) 2024 2023 Revenues 102,502 106,848"),
                 ("Sustainability Report", "Nothing here 4,000")]
        near, types, _ = L.mine_answer(pages, printed_forms(102502e6, CURRENCY_SCALES))
        self.assertIn("revenues", near)
        self.assertEqual(types, {"Annual Report"})
        near, _, _ = L.mine_answer([("x", "Direct GHG emissions (Scope 1) tCO2e 24,442")], printed_forms(24442, EMISSION_SCALES))
        self.assertIn("direct ghg emissions", near)

    def test_suggest_needs_spread_and_lift(self):
        counts = collections.Counter({"capex": 10, "annual": 10, "rare": 2})
        comps = {"capex": set("abcdefghij"), "annual": set("abcdefghij"), "rare": set("ab")}
        bg = collections.Counter({"annual": 900, "capex": 5})
        got = [x[0] for x in L.suggest(counts, comps, bg, 1000, 20, {"capex"})]
        self.assertEqual(got, ["capex"])                                            # 'annual' is everywhere, 'rare' too rare


if __name__ == "__main__":
    unittest.main()
