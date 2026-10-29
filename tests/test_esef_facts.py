"""Offline tests for esef_facts.py: xBRL-JSON facts read into sec_facts.py's shape (group totals only,
annual durations, the end of a period as its last day) and scored with the same rule."""
import os, sys, unittest

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import esef_facts as E  # noqa: E402
import sec_facts as S  # noqa: E402

LEI = "lei:529900ABCDEFGHIJKL12"
OIM = {"facts": {
    "f1": {"value": "38153000000", "decimals": -6, "dimensions": {"concept": "ifrs-full:Revenue", "entity": LEI,
           "period": "2024-01-01T00:00:00/2025-01-01T00:00:00", "unit": "iso4217:EUR"}},
    "f2": {"value": "36000000000", "decimals": -6, "dimensions": {"concept": "ifrs-full:Revenue", "entity": LEI,
           "period": "2023-01-01T00:00:00/2024-01-01T00:00:00", "unit": "iso4217:EUR"}},
    "f3": {"value": "9000000000", "decimals": -6, "dimensions": {"concept": "ifrs-full:Revenue", "entity": LEI,
           "period": "2024-01-01T00:00:00/2025-01-01T00:00:00", "unit": "iso4217:EUR",
           "ifrs-full:SegmentsAxis": "x:EuropeMember"}},                                   # a segment
    "f4": {"value": "9500000000", "decimals": -6, "dimensions": {"concept": "ifrs-full:Revenue", "entity": LEI,
           "period": "2024-10-01T00:00:00/2025-01-01T00:00:00", "unit": "iso4217:EUR"}},     # a quarter
    "f5": {"value": "1844095000000", "decimals": -6, "dimensions": {"concept": "ifrs-full:Revenue", "entity": LEI,
           "period": "2024-04-01T00:00:00/2025-04-01T00:00:00", "unit": "iso4217:JPY"}},     # March year end
    "f6": {"value": "1.23", "dimensions": {"concept": "ifrs-full:BasicEarningsLossPerShare", "entity": LEI,
           "period": "2024-01-01T00:00:00/2025-01-01T00:00:00", "unit": "iso4217:EUR/xbrli:shares"}}}}


class Reading(unittest.TestCase):
    def test_period(self):
        s, last, days = E.period_days("2024-01-01T00:00:00/2025-01-01T00:00:00")
        self.assertEqual((str(last), days), ("2024-12-31", 366))
        self.assertIsNone(E.period_days("2024-12-31T00:00:00"))

    def test_group_annual_revenue_only(self):
        facts = E.annual_facts(OIM, "2025-03-10")
        vals = sorted(f[4] for f in facts)
        self.assertEqual(vals, [36_000_000_000.0, 38_153_000_000.0, 1_844_095_000_000.0])
        self.assertIn((2024, "JPY"), {(f[2], f[5]) for f in facts})                    # a March 2025 year end is 2024

    def test_scored_with_the_sec_rule(self):
        facts = E.annual_facts(OIM, "2025-03-10")
        self.assertTrue(S.score({"year": 2024, "value": 38_153_000_000.0, "unit": "EUR"}, facts)["match"])
        self.assertTrue(S.score({"year": 2023, "value": 36_000_000_000.0, "unit": "EUR"}, facts)["match"])
        self.assertEqual(E.annual_facts(None), [])


if __name__ == "__main__":
    unittest.main()
