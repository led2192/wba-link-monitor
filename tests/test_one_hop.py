import os
import unittest
from unittest import mock

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")

import one_hop as O
from anchor_capture_eval import Held

HUB = """<html><body>
<a href="/en/annual-report-2025">Annual Report 2025</a>
<a href="/en/governance">Governance documents</a>
</body></html>"""
MICRO = """<html><body>
<a href="/docs/acme-annual-report-2025.pdf">Annual Report 2025 (PDF)</a>
<a href="/docs/acme-annual-report-2019.pdf">Annual Report 2019</a>
<a href="/docs/agm-minutes-2025.pdf">Minutes of the AGM</a>
</body></html>"""
GOV = """<html><body>
<a href="/docs/code-of-conduct.pdf">Code of Conduct</a>
<a href="/docs/acme-human-rights-policy.pdf">Human Rights Policy</a>
</body></html>"""


class Resp:
    def __init__(self, code, ctype, body, url):
        self.status_code, self.headers, self._body, self.url, self.encoding = code, {"Content-Type": ctype}, body, url, "utf-8"
    def iter_content(self, n):
        yield self._body
    def close(self):
        pass


class Session:
    def get(self, url, **kw):
        page = {"/reports": HUB, "/en/annual-report-2025": MICRO, "/en/governance": GOV}
        for k, v in page.items():
            if url.endswith(k):
                return Resp(200, "text/html", v.encode(), url)
        return Resp(404, "text/html", b"", url)


class Rule(unittest.TestCase):
    def test_keep(self):
        self.assertEqual(O.keep("Annual Report 2025 (PDF)", "Annual Report 2025", "https://a.com/x.pdf", set())[0], "Annual Report")
        self.assertIsNone(O.keep("Annual Report 2019", "Archive", "https://a.com/ar-2019.pdf", set()))       # too old
        self.assertIsNone(O.keep("Minutes of the AGM", "Governance", "https://a.com/agm-2025.pdf", set()))   # not a key type
        self.assertIsNone(O.keep("Code of Conduct", "Governance", "https://a.com/coc.pdf", set()))           # not lacking
        self.assertEqual(O.keep("Code of Conduct", "Governance", "https://a.com/coc.pdf", {"Code of Conduct"})[0], "Code of Conduct")

    def test_dry_run_cases(self):
        # 5 Nov 2026 dry run: a plan named "annual" is not an annual report
        self.assertIsNone(O.keep("HKMA Consolidated Annual Open Data Plan 2026-28", "Other information",
                                 "https://hkma.gov.hk/x/HKMA_Open_Data_Plan_2026_28.pdf", set()))
        # an older report listed on a 2025 microsite keeps its own year
        self.assertIsNone(O.keep("Download", "Annual Report 2025", "https://kic.kr/annual-report/2025/en/pdf/KIC_2022_en.pdf", set()))
        self.assertEqual(O.keep("Download", "Annual Report 2025", "https://kic.kr/ar/pdf/KIC_2025_en.pdf", set()), ("Annual Report", 2025))
        # wordless links on an archive page do not inherit the parent's type
        self.assertIsNone(O.keep("Download", "Annual Report", "https://x.com/attach/2026/07/Up_admin_1.pdf", set(), may_inherit=False))

    def test_says_nothing(self):
        self.assertTrue(O.says_nothing("Download (PDF, 3 MB)"))
        self.assertTrue(O.says_nothing("Full report"))
        self.assertTrue(O.says_nothing(""))
        self.assertFalse(O.says_nothing("Minutes of the AGM"))
        self.assertEqual(O.keep("Download (PDF, 3 MB)", "Sustainability Report 2024", "https://a.com/x.pdf", set())[0],
                         "Sustainability Report")

    def test_company_finds_keeps_only_key_recent_and_lacking(self):
        pages = [{"url": "https://acme.com/reports", "type": "reports_hub", "company": "Acme"}]
        finds, c = O.company_finds("PT_1", pages, Held(), {"Human Rights Policy"}, 12, 10, 9e12,
                                   session_factory=Session, pause=0)
        urls = sorted(f["document_url"] for f in finds)
        self.assertEqual(urls, ["https://acme.com/docs/acme-annual-report-2025.pdf",
                                "https://acme.com/docs/acme-human-rights-policy.pdf"])
        self.assertTrue(all(" > " in f["link_text"] for f in finds))
        self.assertEqual(finds[0]["found_on"], "https://acme.com/reports")        # linked to the monitored page

    def test_per_company_cap(self):
        pages = [{"url": "https://acme.com/reports", "type": "reports_hub", "company": "Acme"}]
        finds, _ = O.company_finds("PT_1", pages, Held(), {"Human Rights Policy"}, 12, 1, 9e12,
                                   session_factory=Session, pause=0)
        self.assertEqual(len(finds), 1)

    def test_room_under_the_guard(self):
        rows = [{"fields": {"date": "2026-11-04", "records_total": 409_000}},
                {"fields": {"date": "2026-11-03", "records_total": 420_000}}]
        with mock.patch.object(O, "fetch", return_value=rows):
            left, total = O.room({}, "app")
        self.assertEqual(total, 409_000)                           # the latest reading, not the largest
        self.assertEqual(left, int(0.85 * O.RECORD_LIMIT) - 409_000)
        with mock.patch.object(O, "fetch", return_value=[]):
            self.assertEqual(O.room({}, "app"), (None, None))

    def test_library_rows_match_the_harvester_ids(self):
        import hashlib
        from ids import normalize
        f = {"wba_id": "PT_1", "company_name": "Acme", "document_url": "https://acme.com/docs/a-2025.pdf",
             "found_on": "https://acme.com/reports", "page_type": "reports_hub", "link_text": "x > y"}
        r = O.library_rows([f])[0]
        self.assertEqual(r["library_id"], "PT_1-" + hashlib.sha1(normalize(f["document_url"]).encode()).hexdigest()[:10])
        self.assertNotIn("source_page", r)                         # resolved to a real record id at write time


if __name__ == "__main__":
    unittest.main()
