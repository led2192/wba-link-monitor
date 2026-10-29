"""Offline tests for sec_facts.py: which XBRL revenue fact the rule picks for a WBA year, and how it is
scored. The companyfacts shape is the SEC's (facts -> namespace -> concept -> units -> rows)."""
import os, sys, unittest

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sec_facts as S  # noqa: E402


def row(start, end, val, form="10-K", filed="2025-02-20"):
    return {"start": start, "end": end, "val": val, "form": form, "filed": filed, "fy": int(end[:4]), "fp": "FY"}


FACTS = {"facts": {
    "us-gaap": {
        "Revenues": {"units": {"USD": [
            row("2024-01-01", "2024-12-31", 102_502_000_000),
            row("2023-01-01", "2023-12-31", 106_848_000_000, filed="2024-02-21"),
            row("2023-01-01", "2023-12-31", 106_900_000_000, filed="2025-02-20"),          # restated a year later
            row("2024-10-01", "2024-12-31", 25_000_000_000, form="10-Q")]}},               # a quarter
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            row("2024-01-01", "2024-12-31", 101_000_000_000)]}},
        "EarningsPerShareBasic": {"units": {"USD/shares": [row("2024-01-01", "2024-12-31", 3.1)]}}},
    "ifrs-full": {"Revenue": {"units": {"JPY": [
        row("2024-04-01", "2025-03-31", 1_844_095_000_000, form="20-F", filed="2025-06-20")]}}}}}


class Rule(unittest.TestCase):
    def test_wba_year(self):
        self.assertEqual(S.wba_year("2025-03-31"), 2024)
        self.assertEqual(S.wba_year("2024-12-31"), 2024)
        self.assertEqual(S.wba_year("2024-06-30"), 2023)

    def test_only_annual_revenue_facts(self):
        facts = S.annual_facts(FACTS)
        self.assertFalse(any(f[6] == "10-Q" for f in facts))
        self.assertFalse(any("EarningsPerShare" in f[1] for f in facts))
        self.assertIn(2024, {f[2] for f in facts if f[5] == "JPY"})                     # a March 2025 year end is 2024

    def test_choice_prefers_total_revenue_and_the_first_filing(self):
        facts = S.annual_facts(FACTS)
        self.assertEqual(S.choose(facts, 2024)[4], 102_502_000_000)                     # Revenues over contract revenue
        self.assertEqual(S.choose(facts, 2023)[4], 106_848_000_000)                     # as first filed, not restated

    def test_score(self):
        facts = S.annual_facts(FACTS)
        ok = S.score({"year": 2024, "value": 102_502_000_000.0, "unit": "USD"}, facts)
        self.assertTrue(ok["match"] and ok["any_match"])
        other = S.score({"year": 2024, "value": 101_000_000_000.0, "unit": "USD"}, facts)
        self.assertFalse(other["match"]); self.assertTrue(other["any_match"])           # the narrower concept
        cur = S.score({"year": 2024, "value": 102_502_000_000.0, "unit": "EUR"}, facts)
        self.assertFalse(cur["match"])                                                  # another currency
        none = S.score({"year": 2019, "value": 1.0, "unit": "USD"}, facts)
        self.assertFalse(none["has_year"])
        self.assertEqual(S.annual_facts(None), [])


if __name__ == "__main__":
    unittest.main()
