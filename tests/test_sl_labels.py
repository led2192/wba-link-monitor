"""Offline tests for sl_labels.py. Run: QA_STRICT=true python3 -m unittest tests/test_sl_labels.py"""
import os, sys, unittest

os.environ["QA_STRICT"] = "true"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sl_labels as L  # noqa: E402


def src(**k):
    d = {"src": "2025", "rec": "recS", "wba": "PT_1", "type": "Annual Report", "year": 2024,
         "filename": "acme-annual-report-2024.pdf", "size": 1000}
    d.update(k); return d


def row(**k):
    d = {"_id": "recR", "library_id": "PT_1-abc", "wba_id": "PT_1", "file": [{"filename": "x.pdf", "size": 1000}],
         "source_type": "Annual Report", "source_type_check": "Annual Report", "year": 2024}
    d.update(k); return d


class SlTests(unittest.TestCase):
    def test_generic_names_do_not_match(self):
        self.assertFalse(L.specific_name("annual-report.pdf"))
        self.assertFalse(L.specific_name("download.pdf"))
        self.assertTrue(L.specific_name("acme-annual-report-2024.pdf"))

    def test_match_by_size_or_specific_name_same_company_only(self):
        rows = [row(), row(_id="recN", library_id="PT_1-n", file=[{"filename": "acme-annual-report-2024.pdf", "size": 5}]),
                row(_id="recO", wba_id="PT_2", file=[{"filename": "y.pdf", "size": 2000}])]
        pairs = L.match([src(), src(rec="r2", size=7), src(rec="r3", wba="PT_3", size=2000)], rows)
        self.assertEqual([(s["rec"], r["_id"]) for s, r in pairs], [("recS", "recR"), ("r2", "recN")])

    def test_doubt_rules(self):
        self.assertEqual(L.doubt(src(), row()), "")
        self.assertIn("full text says 2023", L.doubt(src(), row(fiscal_year=2023, year_score=85)))
        self.assertEqual(L.doubt(src(), row(fiscal_year=2023, year_score=70)), "")
        self.assertIn("both say Sustainability Report",
                      L.doubt(src(), row(source_type="Sustainability Report", source_type_check="Sustainability Report")))
        # same check (Integrated vs Annual both satisfy ANNUAL? Integrated also SUST) -> different checks -> doubt
        self.assertEqual(L.doubt(src(type="ESG Report"), row(source_type="Sustainability Report",
                                                              source_type_check="Sustainability Report")), "")

    def test_label_row_sure_only_for_2025_without_doubt(self):
        lid, r = L.label_row(src(), row(), "2026-10-22")
        self.assertEqual(lid, "PT_1-abc|source_library|2025")
        self.assertTrue(r["sure"]); self.assertEqual(r["true_year"], 2024); self.assertEqual(r["true_type"], "Annual Report")
        self.assertFalse(L.label_row(src(src="2024"), row(), "2026-10-22")[1]["sure"])
        _, p = L.label_row(src(type="Code of Conduct", year=2021), row(), "2026-10-22")
        self.assertNotIn("true_year", p)

    def test_score_exact_and_check_level(self):
        pairs = [(src(), row()), (src(type="Integrated Report"), row(source_type_check="Annual Report")),
                 (src(type="ESG Report"), row(source_type_check="Sustainability Report"))]
        n, acc, chk, table = L.score(pairs, lambda r: r["source_type_check"], lambda s: s["type"])
        self.assertEqual(n, 3); self.assertAlmostEqual(acc, 1 / 3)
        self.assertAlmostEqual(chk, 2 / 3)   # ESG vs Sustainability same check; Integrated vs Annual not
        self.assertEqual(table["Integrated Report"]["top_confusion"], "Annual Report")


class HouseRules(unittest.TestCase):
    def test_modern_slavery_statement_is_a_social_report(self):
        import sl_labels as L
        self.assertEqual(L.house_type("Policy Documents", "Takeda fy2024_human_rights_and_modern_slavery_statement.pdf")[0],
                         "Social Reports")
        self.assertEqual(L.house_type("Human Rights Policy", "Modern Slavery Act Statement 2024")[0], "Social Reports")

    def test_modern_slavery_policy_stays_a_policy(self):
        import sl_labels as L
        self.assertEqual(L.house_type("Human Rights Policy", "human-rights-and-modern-slavery-policy.pdf")[0],
                         "Human Rights Policy")

    def test_human_rights_statement_is_the_policy(self):
        import sl_labels as L
        self.assertEqual(L.house_type("Policy Documents", "statement-human-rights.pdf")[0], "Human Rights Policy")
        self.assertEqual(L.house_type("Policy Documents", "Anti-bribery policy")[0], "Policy Documents")


if __name__ == "__main__":
    unittest.main()
