"""archive_recheck.py: what the archive's index says of a capture, offline."""
import unittest

import archive_recheck as A

H = ["timestamp", "mimetype", "statuscode", "length"]


class Recheck(unittest.TestCase):
    def test_split(self):
        self.assertEqual(A.split("https://web.archive.org/web/20241109092820id_/https://x.example/a/Report.pdf"), ("20241109092820", "https://x.example/a/Report.pdf"))
        self.assertEqual(A.split("http://web.archive.org/web/2023/https://x.example/r.pdf"), ("2023", "https://x.example/r.pdf"))
        self.assertIsNone(A.split("https://x.example/r.pdf"))

    def test_larger_capture_and_its_row(self):
        from datetime import date
        rows = [H, ["1", "application/pdf", "200", "900"], ["2", "application/pdf", "200", "5000"], ["3", "application/pdf", "200", "4000"]]
        self.assertEqual(A.larger_capture("1", rows), "2")
        self.assertIsNone(A.larger_capture("2", rows)); self.assertIsNone(A.larger_capture("1", []))
        r = A.new_row(("WX-old", "WX", "Zebra Bank", "Annual Report", 2023), "20240102030405", "https://x.example/r.pdf", date(2026, 11, 24))
        self.assertEqual((r["document_url"], r["wba_id"], r["source_type"], r["doc_year"], r["first_seen"]), ("https://web.archive.org/web/20240102030405id_/https://x.example/r.pdf", "WX", "Annual Report", "2023", "2026-11-24"))
        self.assertRegex(r["library_id"], r"^WX-[0-9a-f]{10}$"); self.assertIn("held as WX-old", r["Reviewer notes"])
        self.assertEqual(A.new_row(("WX-old", "WX", "Zebra Bank", "Annual Report", 2023), "20240102030405", "https://x.example/r.pdf")["library_id"], r["library_id"])          # the same id on a second run
        self.assertNotIn("doc_year", A.new_row(("a", "WX", "Z", None, None), "20240102030405", "https://x.example/r.pdf"))

    def test_wait(self):
        self.assertEqual((A.wait_of(None), A.wait_of("abc"), A.wait_of("5"), A.wait_of("120"), A.wait_of("9000")), (90, 90, 30, 120, 300))

    def test_verdict(self):
        self.assertEqual(A.verdict("1", []), "no capture answered with a PDF")
        self.assertEqual(A.verdict("1", [H, ["1", "text/html", "200", "900"], ["2", "application/pdf", "404", "10"]]), "no capture answered with a PDF")
        self.assertEqual(A.verdict("1", [H, ["1", "application/pdf", "200", "900"]]), "the held capture is the only PDF")
        self.assertEqual(A.verdict("1", [H, ["1", "application/pdf", "200", "900"], ["2", "application/pdf", "200", "5000"]]), "a larger capture exists")
        self.assertEqual(A.verdict("1", [H, ["1", "application/pdf", "200", "5000"], ["2", "application/pdf", "200", "5100"]]), "no larger capture")          # within 5%
        self.assertEqual(A.verdict("1", [H, ["1", "application/pdf", "200", "5000"], ["2", "application/pdf", "200", "-"]]), "no larger capture")
        self.assertEqual(A.verdict("9", [H, ["1", "application/pdf", "200", "5000"]]), "the held capture is not in the index")
        self.assertEqual(A.verdict("1", [H, ["1", "text/html", "200", "300"], ["2", "application/pdf", "200", "5000"]]), "a larger capture exists")          # the held capture is a page, not the file


if __name__ == "__main__":
    unittest.main()
