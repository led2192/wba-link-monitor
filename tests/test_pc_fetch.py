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


if __name__ == "__main__":
    unittest.main()
