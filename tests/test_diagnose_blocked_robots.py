import os
import unittest
from unittest import mock

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")

import diagnose_blocked as D


class Resp:
    def __init__(self, code, text="", ctype="text/plain"):
        self.status_code, self.text, self.headers = code, text, {"Content-Type": ctype}


def fake_get(url, headers, timeout=20):
    if url == "https://shy.example/robots.txt":
        return Resp(200, "User-agent: *\nDisallow: /reports/\n")
    if url == "https://open.example/robots.txt":
        return Resp(200, "User-agent: *\nAllow: /\n")
    if url == "https://walled.example/robots.txt":
        return Resp(403, "<html>Access denied</html>", "text/html")
    return Resp(404)


class RobotsTally(unittest.TestCase):
    def setUp(self):
        D._site_cache.clear()

    def test_documents_are_checked_against_robots_txt(self):
        with mock.patch.object(D.Site, "get", side_effect=fake_get):
            rows = [D.document_row({"fields": {"library_id": str(i), "wba_id": w, "company_name": w, "document_url": u}})
                    for i, (w, u) in enumerate([("PT_1", "https://shy.example/reports/ar.pdf"),
                                                ("PT_1", "https://shy.example/about/ar.pdf"),
                                                ("PT_2", "https://open.example/ar.pdf"),
                                                ("PT_3", "https://walled.example/ar.pdf")])]
        self.assertEqual([r["robots_allows"] for r in rows], ["no", "yes", "yes", "unknown"])
        line = D.robots_summary(rows, "wba_id", "Documents")[0]
        self.assertIn("| Documents | 4 | 1 (25.0%) | 2 | 1 | 1 companies, 1 sites |", line)

    def test_empty_rows_do_not_divide_by_zero(self):
        self.assertIn("| none | 0 | 0 (0.0%)", D.robots_summary([], "wba_id", "none")[0])


if __name__ == "__main__":
    unittest.main()
