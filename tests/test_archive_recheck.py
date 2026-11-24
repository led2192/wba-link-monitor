"""archive_recheck.py: what the archive's index says of a capture, offline."""
import unittest

import archive_recheck as A

H = ["timestamp", "mimetype", "statuscode", "length"]


class Recheck(unittest.TestCase):
    def test_split(self):
        self.assertEqual(A.split("https://web.archive.org/web/20241109092820id_/https://x.example/a/Report.pdf"), ("20241109092820", "https://x.example/a/Report.pdf"))
        self.assertEqual(A.split("http://web.archive.org/web/2023/https://x.example/r.pdf"), ("2023", "https://x.example/r.pdf"))
        self.assertIsNone(A.split("https://x.example/r.pdf"))

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
