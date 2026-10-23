import unittest
from unittest import mock

import blocked_retry as br

TODAY = "2026-11-02"
ROW = {"_id": "rec1", "document_url": "https://www.clp.com.hk/reports/CLP_Sustainability_Report_2024_en.pdf",
       "found_on": "https://www.clp.com.hk/reports"}


class Helpers(unittest.TestCase):
    def test_distinctive_names(self):
        self.assertTrue(br.distinctive("CLP_Sustainability_Report_2024_en.pdf"))
        self.assertFalse(br.distinctive("report.pdf"))
        self.assertFalse(br.distinctive("download.pdf"))
        self.assertFalse(br.distinctive("3f2a9c0d4e5b6a7f8e9d.pdf"))   # a hash, not a name

    def test_registered_domain(self):
        self.assertEqual(br.registered_domain("www.clp.com.hk"), "clp.com.hk")
        self.assertEqual(br.registered_domain("ir.example.co.jp"), "example.co.jp")
        self.assertEqual(br.registered_domain("investors.acme.com"), "acme.com")

    def test_parse_cdx_newest_first(self):
        js = [["timestamp", "original", "length"], ["20230101000000", "http://a/x.pdf", "900000"],
              ["20240502000000", "https://a/x.pdf", "910000"]]
        self.assertEqual(br.parse_cdx(js)[0][0], "20240502000000")
        self.assertEqual(br.parse_cdx([]), [])

    def test_raw_copy_keeps_original_bytes(self):
        self.assertEqual(br.raw_copy("20240502000000", "https://a/x.pdf"),
                         "https://web.archive.org/web/20240502000000id_/https://a/x.pdf")


class Decide(unittest.TestCase):
    def test_browser_request_wins_and_uploads(self):
        act, f, c = br.decide(ROW, TODAY, headers_fn=lambda r: {"route": "headers", "data": b"%PDF" + b"x" * 30000},
                              archive_fn=lambda r: self.fail("archive should not be tried"))
        self.assertEqual((act, c), ("upload", "headers"))

    def test_archive_copy_attached_by_url(self):
        act, f, c = br.decide(ROW, TODAY, headers_fn=lambda r: {"route": None, "detail": "browser request 403"},
                              archive_fn=lambda r: {"route": "archive", "url": "https://web.archive.org/web/1id_/x",
                                                    "detail": "Internet Archive copy of 2024-05-02"})
        self.assertEqual((act, c), ("attach_url", "archive"))
        self.assertEqual(f["note"], "2026-11-02: attached from an Internet Archive copy of 2024-05-02")

    def test_still_refused_says_what_was_tried(self):
        act, f, c = br.decide(ROW, TODAY, headers_fn=lambda r: {"route": None, "detail": "browser request 403"},
                              archive_fn=lambda r: {"route": None, "detail": "no archive copy"})
        self.assertEqual((act, c), ("note", "still_refused"))
        self.assertIn("browser request 403; no archive copy", f["note"])


class ArchiveRoute(unittest.TestCase):
    def test_variant_on_another_address_when_name_is_distinctive(self):
        calls = []

        def fake_cdx(params):
            calls.append(params)
            if params["matchType"] == "prefix":
                return []
            return [("20240502000000", "https://www.clp.com/content/dam/CLP_Sustainability_Report_2024_en.pdf", 900000)]
        with mock.patch.object(br, "cdx", fake_cdx), mock.patch.object(br, "archive_pdf", lambda u: (True, 4096)):
            res = br.try_archive(ROW)
        self.assertEqual(res["route"], "archive")
        self.assertIn("another address", res["detail"])
        self.assertEqual(calls[1]["url"], "clp.com.hk")

    def test_same_name_required_on_prefix_match(self):
        with mock.patch.object(br, "cdx", lambda p: [("20240101000000", "https://www.clp.com.hk/reports/other.pdf", 900000)]
                               if p["matchType"] == "prefix" else []), \
             mock.patch.object(br, "archive_pdf", lambda u: (True, 4096)):
            self.assertIsNone(br.try_archive(ROW)["route"])

    def test_not_a_pdf_is_rejected(self):
        with mock.patch.object(br, "cdx", lambda p: [("20240101000000", ROW["document_url"], 900000)]), \
             mock.patch.object(br, "archive_pdf", lambda u: (False, 300)):
            self.assertIsNone(br.try_archive(ROW)["route"])


if __name__ == "__main__":
    unittest.main()
