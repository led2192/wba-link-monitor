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

    def test_where_the_analyst_took_the_value_from(self):
        g = {"value": 10_886_000.0, "unit": "tCO2e", "quote": "Direct GHG emissions: 10.886 million tonnes", "page": "84",
             "editor_url": "https://airtable.com/appX/pagY/recZ", "sources": [{"name": "PT_1-2024-ESG", "url": "https://x.com/esg2024.pdf", "year": "2024"}]}
        f = P.row("PT_1", "GMC_00048.1", "Revenue", 2024, FACT, "SEC XBRL", "u", {("PT_1", "GMC_00048.1", 2024): g}, None, NOW)
        self.assertEqual((f["validated_source"], f["validated_source_name"], f["validated_page"], f["validated_record"]),
                         ("https://x.com/esg2024.pdf", "PT_1-2024-ESG", "84", "https://airtable.com/appX/pagY/recZ"))
        self.assertIn("10.886", f["validated_quote"])
        bare = P.validated_fields({"value": 1.0, "editor_url": "#RECORD_ID#"})                       # nothing cited
        self.assertTrue(all(v is None for v in bare.values()))

    def test_plan_never_touches_a_decision(self):
        rows = [P.row(w, "GMC_00048.1", "Revenue", 2024, FACT, "SEC XBRL", "u", {}, None, NOW) for w in ("A", "B", "C")]
        existing = {"B|GMC_00048.1|2024|SEC XBRL": {"id": "recB", "status": "accepted"},
                    "C|GMC_00048.1|2024|SEC XBRL": {"id": "recC", "status": "proposed"}}
        creates, updates, kept = P.plan(rows, existing, NOW)
        self.assertEqual([f["value_id"][0] for f in creates], ["A"])
        self.assertEqual((creates[0]["status"], creates[0]["first_proposed"]), ("proposed", NOW))
        self.assertEqual([u[0] for u in updates], ["recC"]); self.assertNotIn("status", updates[0][1])
        self.assertEqual(kept, 1)

    def test_rows_no_longer_proposed_are_superseded(self):
        rows = [P.row("A", "GMC_00048.1", "Revenue", 2024, FACT, "SEC XBRL", "u", {}, None, NOW)]
        existing = {"B|GMC_00048.1|2024|SEC XBRL": {"id": "recB", "status": "proposed"},
                    "C|GMC_00048.1|2024|SEC XBRL": {"id": "recC", "status": "accepted"},
                    "D|GMC_00048.1|2019|SEC XBRL": {"id": "recD", "status": "proposed"}}
        in_scope = lambda vid: vid.endswith("|2024|SEC XBRL")
        _, updates, _ = P.plan(rows, existing, NOW, in_scope)
        self.assertEqual(updates, [("recB", {"status": "superseded", "last_run": NOW})])

    def test_model_row(self):
        got = {"value": 2804e6, "printed": 2804.0, "scale": 1e6, "library_id": "PT_1-abc", "page": 57, "unit": "EUR"}
        doc = {"id": "recDoc", "url": "https://example.com/ar.pdf", "label": "2024 | Testco | Annual Report"}
        gold = {("PT_1", "GMC_00048.1", 2024): {"value": 2804e6, "unit": "EUR"}}
        f = P.model_row("PT_1", "GMC_00048.1", {"name": "Revenue"}, 2024, got, doc, gold, "recCo", NOW)
        self.assertEqual(f["value_id"], "PT_1|GMC_00048.1|2024|model reader")
        self.assertEqual((f["method"], f["page"], f["document"], f["company"]), ("model reader", 57, ["recDoc"], ["recCo"]))
        self.assertEqual(f["source_url"], "https://example.com/ar.pdf#page=57")
        self.assertIn("Printed as 2,804 (millions) on page 57 of 2024 | Testco | Annual Report", f["detail"])
        self.assertEqual(f["matches_validated"], "match")
        f = P.model_row("PT_2", "GMC_00001.1", {"name": "Scope 1", "unit_family": "tco2e"}, 2024, dict(got, unit=""), {}, {}, None, NOW)
        self.assertEqual((f["unit"], f["source_url"], f["matches_validated"]), ("tCO2e", None, "no validated answer"))
        self.assertNotIn("document", f)

    def test_printed_text_keeps_every_digit(self):
        self.assertEqual(P.printed_text(41709.13), "41,709.13")
        self.assertEqual(P.printed_text(2804.0), "2,804")
        self.assertEqual(P.printed_text(5206474.45), "5,206,474.45")

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
        self.assertEqual(P.sec_url("4962", "0000004962-24-000013", "axp-20231231.htm"),
                         "https://www.sec.gov/ix?doc=/Archives/edgar/data/4962/000000496224000013/axp-20231231.htm")

    def test_how_to_find(self):
        self.assertEqual(P.how_to_find(60515e6), "Search the report for 60,515 (in millions) or 60,515,000 (in thousands), in the income statement.")
        self.assertIn("341 (in millions)", P.how_to_find(341e6))
        self.assertIn("7.9 (in millions)", P.how_to_find(7.9e6))


if __name__ == "__main__":
    unittest.main()
