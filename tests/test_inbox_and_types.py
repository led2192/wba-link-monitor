"""Offline tests: address types for the 23 Oct 2026 cases; add_link is honest about refused PDFs."""
import os, sys, unittest

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import classify_library as C  # noqa: E402


class AddressTypes(unittest.TestCase):
    def t(self, url):
        return C.classify(url)[0]

    def test_sustainability_report_in_the_address(self):
        self.assertEqual(self.t("https://www.clpgroup.com/content/dam/clp-group/channels/sustainability/document/"
                                "sustainability-report/2025/CLP_Sustainability_Report_2025_en.pdf.coredownload.pdf"),
                         "Sustainability Report")

    def test_10q_is_interim_not_10k(self):
        self.assertEqual(self.t("https://www.carnivalcorp.com/wp-content/uploads/2024/08/Filed-2021-3Q-Form-10-Q-VD.pdf"),
                         "Interim Reports")
        self.assertEqual(self.t("https://x.com/sec/form-10-k-2024.pdf"), "10K Form")

    def test_indian_integrated_filing_is_quarterly(self):
        self.assertEqual(self.t("https://www.dlf.in/related-pdf/integrated_filing_financial_q3fy25.pdf"), "Interim Reports")

    def test_unchanged_cases(self):
        self.assertEqual(self.t("https://x.com/ir/annual-report-2024.pdf"), "Annual Report")
        self.assertEqual(self.t("https://x.com/ir/integrated-annual-report-2024.pdf"), "Integrated Report")
        self.assertEqual(self.t("https://x.com/esg-report-2024.pdf"), "ESG Report")


class FakeResp:
    def __init__(self, code):
        self.status_code = code
    def close(self):
        pass


class FakeSess:
    def __init__(self, code):
        self.code = code
    def get(self, *a, **k):
        return FakeResp(self.code)


class AddLink(unittest.TestCase):
    def setUp(self):
        import inbox
        self.I = inbox

    def test_refused_pdf_is_reported_as_blocked(self):
        self.assertEqual(self.I.what_is("https://x.com/report-2025.pdf", FakeSess(403)), ("blocked", 403))

    def test_open_pdf_is_added(self):
        self.assertEqual(self.I.what_is("https://x.com/report-2025.pdf", FakeSess(200))[0], "pdf")

    def test_result_says_the_first_guess(self):
        msg = self.I.expected("https://x.com/docs/sustainability-report-2025.pdf")
        self.assertIn("Sustainability Report 2025", msg)


class FileOntoExistingRow(unittest.TestCase):
    def test_file_with_link_of_fileless_row_patches_that_row(self):
        import inbox as I
        url = "https://www.clpgroup.com/x/CLP_Sustainability_Report_2025_en.pdf"
        writes = []
        I.company_state = lambda wba: ([], [{"id": "recROW", "fields": {"document_url": url, "library_id": "L1"}}])
        I.write = lambda table, method, recs: writes.append((table, method, recs)) or [{"id": "recNEW"}]
        rec = {"id": "recCO", "fields": {"wba_id": "PT_00440", "company_name": "CLP", "add_link": url,
                                         "add_file": [{"url": "https://dl/x.pdf", "filename": "CLP_SR_2025.pdf", "size": 10}]}}
        lines, created, _ = I.handle_company(rec, FakeSess(403), set())
        self.assertEqual([(t, m) for t, m, _ in writes], [(I.LIB, "PATCH")])
        self.assertEqual(writes[0][2][0]["id"], "recROW")
        self.assertIn("existing row", lines[0])


class HumanRightsAddresses(unittest.TestCase):
    def test_human_rights_policies_stay_human_rights(self):
        for u in ("https://x.com/files/policies/human-rights-and-modern-slavery-policy.pdf",
                  "https://x.com/code-engagement/responsabilite-entreprise/statement-human-rights.pdf",
                  "https://www.meiji.com/x/meiji_group_policy_on_human_rights/meiji_group_policy_on_human_rights.pdf"):
            self.assertEqual(C.classify(u)[0], "Human Rights Policy", u)




class ProspectusIsStatutory(unittest.TestCase):
    def test_bond_documents_are_statutory_and_urds_stay_registration(self):
        import classify_library as C
        self.assertEqual(C.classify("https://x.com/bonds/base-prospectus-2024.pdf")[0], "Statutory Documents")
        self.assertEqual(C.classify("https://x.com/debt/final-terms-series-12.pdf")[0], "Statutory Documents")
        self.assertEqual(C.classify("https://x.com/universal-registration-document-2024.pdf")[0], "Registration Document")


if __name__ == "__main__":
    unittest.main()
