"""Offline tests for propose_values.py: deterministic ids, the comparison with the validated answer, and
rows a person decided left alone."""
import os, sys, unittest

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import propose_values as P  # noqa: E402

FACT = (0, "us-gaap:Revenues", 2024, "2024-12-31", 102502e6, "USD", "10-K", "2025-02-20")
NOW = "2026-10-29T12:00:00.000Z"


class Propose(unittest.TestCase):
    def test_row_and_validated_answer(self):
        gold = {("PT_1", "GMC_00048.1", 2024): {"value": 102500e6, "unit": "USD"}}
        f = P.row("PT_1", "GMC_00048.1", "Revenue", 2024, FACT, "SEC XBRL", "u", gold, "recA", NOW)
        self.assertEqual(f["value_id"], "PT_1|GMC_00048.1|2024|SEC XBRL")
        self.assertEqual((f["company"], f["value"], f["unit"], f["matches_validated"]), (["recA"], 102502e6, "USD", "match"))
        gold[("PT_1", "GMC_00048.1", 2024)] = {"value": 102502e3, "unit": "USD"}       # a factor of 1,000
        self.assertEqual(P.row("PT_1", "GMC_00048.1", "Revenue", 2024, FACT, "SEC XBRL", "u", gold, "recA", NOW)["matches_validated"], "differs")
        f = P.row("PT_1", "GMC_00048.1", "Revenue", 2023, FACT, "SEC XBRL", "u", gold, None, NOW)
        self.assertEqual(f["matches_validated"], "no validated answer"); self.assertNotIn("company", f)

    def test_plan_never_touches_a_decision(self):
        rows = [P.row(w, "GMC_00048.1", "Revenue", 2024, FACT, "SEC XBRL", "u", {}, None, NOW) for w in ("A", "B", "C")]
        existing = {"B|GMC_00048.1|2024|SEC XBRL": {"id": "recB", "status": "accepted"},
                    "C|GMC_00048.1|2024|SEC XBRL": {"id": "recC", "status": "proposed"}}
        creates, updates, kept = P.plan(rows, existing, NOW)
        self.assertEqual([f["value_id"][0] for f in creates], ["A"])
        self.assertEqual((creates[0]["status"], creates[0]["first_proposed"]), ("proposed", NOW))
        self.assertEqual([u[0] for u in updates], ["recC"]); self.assertNotIn("status", updates[0][1])
        self.assertEqual(kept, 1)

    def test_years(self):
        from datetime import datetime
        t = datetime(2026, 10, 29)
        self.assertEqual(P.parse_years("2018-", t), list(range(2018, 2026)))
        self.assertEqual(P.parse_years("2024", t), [2024])
        self.assertEqual(P.parse_years("2022,2020-2021", t), [2020, 2021, 2022])

    def test_sec_accession_and_url(self):
        cf = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
            {"end": "2024-12-31", "filed": "2025-02-20", "val": 102502e6, "accn": "0000123456-25-000010"}]}}}}}
        self.assertEqual(P.sec_accession(cf, FACT), "0000123456-25-000010")
        self.assertEqual(P.sec_url("123456", "0000123456-25-000010"), "https://www.sec.gov/Archives/edgar/data/123456/000012345625000010/")
        self.assertIn("type=10-K", P.sec_url("123456", ""))


if __name__ == "__main__":
    unittest.main()
