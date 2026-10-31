"""Offline tests for regulator_filings.py: filing rows are web documents, old rows are relabelled."""
import os, sys, unittest

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import regulator_filings as R  # noqa: E402

SEC = "https://www.sec.gov/Archives/edgar/data/1413447/000141344725000010/nxpi-20241231.htm"


class Filings(unittest.TestCase):
    def test_a_filing_row_is_a_web_document(self):
        comp = {"fields": {"wba_id": "PT_9", "company_name": "NXP"}}
        rows, notes = R.rows_for(comp, [(2024, "Form 10-K", SEC, "2025-02-20")], set(), set(), "SEC")
        self.assertEqual(rows[0]["file_status"], "web_document")
        self.assertEqual(rows[0]["doc_year"], "2024")
        rows, _ = R.rows_for(comp, [(2024, "Form 10-K", SEC, "2025-02-20")], {2024}, set(), "SEC")
        self.assertEqual(rows, [])                                                   # the year is held: gaps only

    def test_mark_web_relabels_only_filings(self):
        got, sent = [{"id": "rec1", "fields": {"document_url": SEC}},
                     {"id": "rec2", "fields": {"document_url": "https://www.sec.gov/ix?doc=/Archives/edgar/data/1/2/x.htm"}}], []
        real_get, real_write = R.at_get, R.at_write
        R.at_get = lambda table, fields, formula=None: got
        R.at_write = lambda table, method, records: sent.extend(records) or records
        try:
            R.mark_web(False); self.assertEqual(sent, [])                            # dry run writes nothing
            R.mark_web(True)
        finally:
            R.at_get, R.at_write = real_get, real_write
        self.assertEqual(sent, [{"id": "rec1", "fields": {"file_status": "web_document"}}])


class Asia(unittest.TestCase):
    def test_codes_from_openfigi(self):
        data = [{"exchCode": "HK", "ticker": "700"}, {"exchCode": "US", "ticker": "TCEHY"}]
        self.assertEqual(R.codes_from_figi(data), "HK:00700")
        data = [{"exchCode": "CG", "ticker": "601857"}, {"exchCode": "HK", "ticker": "857"}, {"exchCode": "CH", "ticker": "601857"}]
        self.assertEqual(R.codes_from_figi(data), "HK:00857; SH:601857")
        self.assertEqual(R.codes_from_figi([{"exchCode": "CS", "ticker": "000333"}]), "SZ:000333")
        self.assertEqual(R.codes_from_figi([{"exchCode": "LN", "ticker": "HSBA"}]), "none")
        self.assertEqual(R.parse_codes("HK:00857; SH:601857"), {"HK": "00857", "SH": "601857"})
        self.assertEqual(R.parse_codes("none"), {})

    def test_report_year(self):
        self.assertEqual(R.report_year("ANNUAL REPORT 2024", "2025-04-08"), 2024)
        self.assertEqual(R.report_year("2024年年度报告", "2025-03-28"), 2024)
        self.assertEqual(R.report_year("2024/25 Annual Report", "2025-07-10"), 2024)
        self.assertEqual(R.report_year("Annual Report", "2025-04-08"), 2024)

    def test_hkex_rows(self):
        import json
        payload = {"result": json.dumps([
            {"FILE_LINK": "/listedco/listconews/sehk/2025/0408/2025040800350.pdf", "TITLE": "ANNUAL REPORT 2024", "DATE_TIME": "08/04/2025 16:30"},
            {"FILE_LINK": "/listedco/listconews/sehk/2025/0408/2025040800351.pdf", "TITLE": "NOTIFICATION LETTER TO REGISTERED SHAREHOLDERS", "DATE_TIME": "08/04/2025 16:31"},
            {"FILE_LINK": "/listedco/listconews/sehk/2025/0408/2025040800352.htm", "TITLE": "ANNUAL REPORT 2024", "DATE_TIME": "08/04/2025 16:32"}])}
        self.assertEqual(R.hkex_rows(payload), [(2024, "Annual report (HKEX)",
                                                 "https://www1.hkexnews.hk/listedco/listconews/sehk/2025/0408/2025040800350.pdf", "2025-04-08")])
        self.assertEqual(R.hkex_rows({}), [])

    def test_cninfo_rows_keep_the_full_report(self):
        payload = {"announcements": [
            {"announcementTitle": "2024年年度报告", "adjunctUrl": "finalpage/2025-03-28/1222911234.PDF", "announcementTime": 1743120000000},
            {"announcementTitle": "2024年年度报告摘要", "adjunctUrl": "finalpage/2025-03-28/1222911235.PDF", "announcementTime": 1743120000000},
            {"announcementTitle": "2024年半年度报告", "adjunctUrl": "finalpage/2024-08-28/1221.PDF", "announcementTime": 1724800000000}]}
        rows = R.cninfo_rows(payload, "SH")
        self.assertEqual(rows, [(2024, "Annual report (SSE)", "https://static.cninfo.com.cn/finalpage/2025-03-28/1222911234.PDF", "2025-03-28")])

    def test_exchange_pdfs_are_not_web_documents(self):
        comp = {"fields": {"wba_id": "PT_8", "company_name": "PetroChina"}}
        rows, _ = R.rows_for(comp, [(2024, "Annual report (SSE)", "https://static.cninfo.com.cn/x.PDF", "2025-03-28")], set(), set(), "CNINFO")
        self.assertNotIn("file_status", rows[0])
        self.assertNotIn("web document", rows[0]["Reviewer notes"])


if __name__ == "__main__":
    unittest.main()
