import unittest

import pc_fetch as P


class Plan(unittest.TestCase):
    def test_outcomes(self):
        big = b"%PDF" + b"x" * (P.UPLOAD_MAX + 10)
        self.assertEqual(P.plan(403, b"", True), "refused")
        self.assertEqual(P.plan(None, b"", False), "refused")
        self.assertEqual(P.plan(200, b"<html>", True), "not_pdf")
        self.assertEqual(P.plan(200, b"%PDF" + b"x" * 30000, True), "upload")
        self.assertEqual(P.plan(200, big, True), "read_here")
        self.assertEqual(P.plan(200, b"%PDF-1.4 tiny", True), "too_small")
        self.assertEqual(P.plan(200, b"%PDF" + b"x" * 100, False), "too_big")

    def test_recent_reports_first_then_oldest_retry(self):
        rows = [{"effective_type": "Policy Documents", "year": 2025},
                {"effective_type": "Annual Report", "year": 2024, "retried_on": "2026-11-01"},
                {"effective_type": "Sustainability Report", "year": 2024}]
        out = sorted(rows, key=P.order_key)
        self.assertEqual([r["effective_type"] for r in out], ["Sustainability Report", "Annual Report", "Policy Documents"])

    def test_filename(self):
        self.assertEqual(P.filename_for("https://x.com/a/CLP%20Report%202024.pdf"), "CLP_Report_2024.pdf")
        self.assertEqual(P.filename_for("https://x.com/download?id=3", "lib1"), "download.pdf")


class Pages(unittest.TestCase):
    def test_page_links_written_like_the_monitor(self):
        from unittest import mock
        html = '<a href="/files/annual-report-2025.pdf">Annual report 2025</a><a href="/files/esg-2025.pdf">ESG</a>'

        class R:
            status_code = 200; text = html; url = "https://www.acme.com/investors/reports"
        rec = {"id": "rec1", "fields": {"url": "https://www.acme.com/investors/reports", "type": "reports_hub",
                                        "wba_id": "PT_1", "seen_links": "https://www.acme.com/files/esg-2025.pdf",
                                        "content_hash": "x"}}
        with mock.patch.object(P.requests, "get", return_value=R()), mock.patch.object(P, "SITE_PAUSE", 0):
            upd, what = P.read_page(rec)
        self.assertEqual(what, "new_links")
        self.assertIn("annual-report-2025.pdf", upd["seen_links"])
        self.assertIn("annual-report-2025.pdf", upd["new_links"])
        self.assertIn("pc_checked", upd)
        self.assertNotIn("status", upd)

    def test_refused_page(self):
        from unittest import mock

        class R:
            status_code = 403; text = ""; url = ""
        rec = {"id": "rec1", "fields": {"url": "https://www.acme.com/r", "type": "reports_hub"}}
        with mock.patch.object(P.requests, "get", return_value=R()), mock.patch.object(P, "SITE_PAUSE", 0):
            self.assertEqual(P.read_page(rec)[1], "refused")


class BrowserPass(unittest.TestCase):
    def run_main(self, browser_cls, robots=True):
        from unittest import mock
        import os, tempfile
        rows = [{"_id": "r1", "document_url": "https://a.com/x.pdf", "effective_type": "Annual Report", "year": 2024},
                {"_id": "r2", "document_url": "https://b.com/y.pdf", "effective_type": "Annual Report", "year": 2024}]
        pdf = b"%PDF" + b"x" * 30000
        fetches = {"https://a.com/x.pdf": (200, pdf, True), "https://b.com/y.pdf": (403, b"", True)}
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {"AIRTABLE_TOKEN": "t", "AIRTABLE_BASE": "b"}), \
             mock.patch.object(P, "fetch_rows", lambda *a: rows), \
             mock.patch.object(P, "fetch", lambda u, r=None: fetches[u]), \
             mock.patch.object(P, "robots_allows", lambda u: robots), \
             mock.patch.object(P, "Browser", browser_cls), \
             mock.patch("sys.argv", ["pc_fetch.py", "--pages", "0"]):
            os.chdir(d)
            try:
                P.main()
                return open("pc_fetch.md", encoding="utf-8").read()
            finally:
                os.chdir(cwd)

    class B:
        def __enter__(self): return self
        def __exit__(self, *e): pass
        def fetch_pdf(self, url, referer=None): return 200, b"%PDF" + b"y" * 30000, True

    def test_browser_reaches_what_requests_could_not(self):
        md = self.run_main(self.B)
        self.assertIn("| attached (5 MB or less) | 2 |", md)
        self.assertIn("| of which reached only by the browser (Edge, no window) | 1 |", md)

    def test_robots_txt_disallow_is_respected(self):
        md = self.run_main(self.B, robots=False)
        self.assertIn("| of which skipped because robots.txt disallows them | 1 |", md)
        self.assertIn("| attached (5 MB or less) | 1 |", md)

    def test_no_browser_on_the_computer_counts_as_refused(self):
        class NoB:
            def __enter__(self): raise RuntimeError("msedge not found")
            def __exit__(self, *e): pass
        md = self.run_main(NoB)
        self.assertIn("| still refused | 1 |", md)
        self.assertIn("not available (RuntimeError: msedge not found)", md)


class Robots(unittest.TestCase):
    def test_disallow_and_allow(self):
        from unittest import mock

        class R:
            status_code = 200
            text = "User-agent: *\nDisallow: /private/\n"
        P._robots.clear()
        with mock.patch.object(P.requests, "get", return_value=R()):
            self.assertFalse(P.robots_allows("https://c.com/private/r.pdf"))
            self.assertTrue(P.robots_allows("https://c.com/reports/r.pdf"))




class Deadlines(unittest.TestCase):
    def test_pages_get_the_end_of_the_run(self):
        self.assertEqual(P.deadlines(0, 90, 25, 150), (65 * 60, 90 * 60))

    def test_no_pages_files_use_the_whole_run(self):
        self.assertEqual(P.deadlines(0, 90, 25, 0), (90 * 60, 90 * 60))

    def test_reserve_at_most_half(self):
        self.assertEqual(P.deadlines(0, 30, 25, 150), (15 * 60, 30 * 60))


if __name__ == "__main__":
    unittest.main()
