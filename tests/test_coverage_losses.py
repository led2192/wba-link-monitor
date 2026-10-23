"""Offline tests: company_coverage names the document behind a lost completeness check."""
import os, sys, unittest

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import company_coverage as C  # noqa: E402


class LossTests(unittest.TestCase):
    def stats(self):
        c = C.empty_stats()
        c["check_doc"] = {"Code of Conduct": "recCOC"}
        c["fam_best"] = {"sust": (2024, "recSUS"), "annual": (2023, "recANN")}
        return c

    def test_check_docs_lists_passing_documents_only(self):
        out = C.check_docs(self.stats(), 2024, ["Annual report (recent)", "Human Rights Policy"])
        self.assertEqual(C.parse_check_docs(out), {"Code of Conduct": "recCOC",
                                                   "Sustainability report (recent)": "recSUS"})

    def test_reasons(self):
        w = "PT_1"
        self.assertEqual(C.why_lost("Code of Conduct", w, None, 2024), "the document was deleted")
        f = {"wba_id": w, "doc_label": "CoC", "discard": True, "discard_reason": {"name": "Duplicate of another document"}}
        self.assertIn("discarded (Duplicate", C.why_lost("Code of Conduct", w, f, 2024))
        f = {"wba_id": "PT_2", "doc_label": "CoC", "source_type_check": "Code of Conduct"}
        self.assertIn("moved to PT_2", C.why_lost("Code of Conduct", w, f, 2024))
        f = {"wba_id": w, "doc_label": "CoC", "source_type_check": "Policy Documents"}
        self.assertIn("type is now Policy Documents", C.why_lost("Code of Conduct", w, f, 2024))
        f = {"wba_id": w, "doc_label": "AR", "source_type_check": "Annual Report", "year": 2023,
             "year_source": "5 document text (medium)"}
        self.assertIn("year is now 2023 (5 document text", C.why_lost("Annual report (recent)", w, f, 2024))


class GapMessageTests(unittest.TestCase):
    def test_each_gap_gets_its_own_line(self):
        os.environ["QA_STRICT"] = "true"
        import qa_sync as S
        f = {"wba_id": "PT_1", "_id": "recC", "gap_pages": ["Code of Conduct", "Annual report (recent)"],
             "gaps": "No annual report from 2024 or later (latest held: 2023).\nNo Code of Conduct.",
             "overdue_note": "Sustainability report 2025 expected",
             "completeness_change": "2026-10-24 lost Code of Conduct: CoC: type is now Policy Documents"}
        msgs = {d: ctx["message"] for _, d, _, _, ctx in S.derive_company(f)}
        self.assertEqual(msgs["no_annual_report"], "No annual report from 2024 or later (latest held: 2023).")
        self.assertEqual(msgs["no_coc"], "No Code of Conduct. | CoC: type is now Policy Documents")


if __name__ == "__main__":
    unittest.main()
