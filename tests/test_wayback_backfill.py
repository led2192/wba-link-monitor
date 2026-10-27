"""Offline tests for wayback_backfill.py: which archived PDFs become library rows, how they are
identified, and that nothing already held (live address, archived address, tombstone) comes back."""
import os, sys, unittest
from unittest import mock

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wayback_backfill as W  # noqa: E402
from ids import doc_id  # noqa: E402

# Apax Partners, 27 Oct 2026: the archive holds Edition 2 (2015 capture) and Edition 8 (2021 capture)
APAX = [("20150905084903", "http://www.apax.com/media/485788/apax-partners-sustainability-report-edition-2.pdf", 2_400_000),
        ("20210315153803", "https://www.apax.com/media/2476/apax-esg-report_final.pdf", 5_100_000),
        ("20140701102437", "http://www.apax.com/media/485644/001125_sustainability.pdf", 3_000_000),
        ("20191001000000", "https://www.apax.com/media/2510/apax-sustainability-report_edition-8.pdf", 4_000_000),
        ("20160101000000", "https://www.apax.com/media/1/kosher_certificate_2016.pdf", 160_000),
        ("20170101000000", "https://www.apax.com/media/2/annual-review-2016.pdf", 90_000),
        ("20180101000000", "https://www.apax.com/media/3/press-release-q1.pdf", 900_000),
        ("20120101000000", "https://www.apax.com/media/4/annual-report-2030.pdf", 900_000)]


class Candidates(unittest.TestCase):
    def test_report_family_only_big_enough_oldest_first(self):
        rows = W.candidates("PT_00117", "Apax", APAX, set(), this_year=2026)
        urls = [W.original_of(r["document_url"]) for r in rows]
        # oldest capture first; the certificate (type), the 90 KB review (size), the press release
        # (type) and 001125_sustainability.pdf (a bare word is not a type for the URL classifier; the
        # archive's own search reads first pages and finds it, this script does not yet) are out; the
        # "2030" annual report is in but undated (a year after the capture)
        self.assertEqual(urls, ["https://www.apax.com/media/4/annual-report-2030.pdf",
                                "http://www.apax.com/media/485788/apax-partners-sustainability-report-edition-2.pdf",
                                "https://www.apax.com/media/2510/apax-sustainability-report_edition-8.pdf",
                                "https://www.apax.com/media/2476/apax-esg-report_final.pdf"])
        self.assertNotIn("doc_year", rows[0])
        r = rows[1]
        self.assertEqual(r["source_type"], "Sustainability Report")
        self.assertEqual(r["document_url"], "https://web.archive.org/web/20150905084903id_/"
                         "http://www.apax.com/media/485788/apax-partners-sustainability-report-edition-2.pdf")
        self.assertIn("(captured 2015-09-05)", r["Reviewer notes"])
        self.assertIn("Original address: http://www.apax.com/media/485788/", r["Reviewer notes"])
        self.assertTrue(r["library_id"].startswith("PT_00117-") and len(r["library_id"]) == 19)
        self.assertNotIn("doc_year", r)                                        # no year in that file name

    def test_year_after_the_capture_is_dropped(self):
        rows = W.candidates("X", "X", [("20120101000000", "https://x.com/annual-report-2030.pdf", 900_000)], set(), 2026)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("doc_year", rows[0])
        rows = W.candidates("X", "X", [("20200301000000", "https://x.com/annual-report-2019.pdf", 900_000)], set(), 2026)
        self.assertEqual(rows[0]["doc_year"], "2019")

    def test_held_documents_do_not_come_back(self):
        live = doc_id("PT_00117", "https://www.apax.com/media/2510/apax-sustainability-report_edition-8.pdf")
        arch = doc_id("PT_00117", W.archive_url("20150905084903", "http://www.apax.com/media/485788/apax-partners-sustainability-report-edition-2.pdf"))
        rows = W.candidates("PT_00117", "Apax", APAX, {live, arch}, 2026)
        self.assertEqual([W.original_of(r["document_url"]) for r in rows],
                         ["https://www.apax.com/media/4/annual-report-2030.pdf",
                          "https://www.apax.com/media/2476/apax-esg-report_final.pdf"])

    def test_same_file_under_http_and_https_is_one_row(self):
        rows = W.candidates("X", "X", [("20150101000000", "http://www.x.com/sustainability-report-2014.pdf", 900_000),
                                       ("20160101000000", "https://x.com/sustainability-report-2014.pdf", 900_000)], set(), 2026)
        self.assertEqual(len(rows), 1)

    def test_cap_per_company(self):
        rows = [(f"2015010100{i:04d}", f"https://x.com/r/sustainability-report-{2000 + i % 20}-{i}.pdf", 900_000) for i in range(100)]
        self.assertEqual(len(W.candidates("X", "X", rows, set(), 2026)), W.MAX_PER_COMPANY)


class Helpers(unittest.TestCase):
    def test_archive_url_round_trip(self):
        u = W.archive_url("20150905084903", "http://www.apax.com/a.pdf")
        self.assertEqual(u, "https://web.archive.org/web/20150905084903id_/http://www.apax.com/a.pdf")
        self.assertEqual(W.original_of(u), "http://www.apax.com/a.pdf")
        self.assertIsNone(W.original_of("https://www.apax.com/a.pdf"))

    def test_shared_hosts(self):
        self.assertTrue(W.shared_host("d18rn0p25nwr6d.cloudfront.net"))
        self.assertTrue(W.shared_host("s2.q4cdn.com"))
        self.assertFalse(W.shared_host("www.apax.com"))

    def test_cdx_rows_parses_json_and_gives_up_quietly(self):
        class R:
            def __init__(self, status, body=None):
                self.status_code, self._b = status, body
            def json(self):
                if self._b is None:
                    raise ValueError
                return self._b
        sess = mock.Mock()
        sess.get.return_value = R(200, [["timestamp", "original", "length"], ["20150905084903", "http://x/a.pdf", "123"]])
        self.assertEqual(W.cdx_rows("x.com", sess), [("20150905084903", "http://x/a.pdf", 123)])
        self.assertEqual(sess.get.call_args.kwargs["params"]["matchType"], "domain")
        sess.get.return_value = R(200, [])
        self.assertEqual(W.cdx_rows("x.com", sess), [])
        sess.get.return_value = R(404)
        self.assertEqual(W.cdx_rows("x.com", sess), [])


if __name__ == "__main__":
    unittest.main()
