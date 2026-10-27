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


class LinkText(unittest.TestCase):
    def test_link_text_types_a_file_whose_address_says_nothing(self):
        rows = [("20140701102437", "http://www.apax.com/media/485644/001125_sustainability.pdf", 3_000_000),
                ("20140701102437", "http://www.apax.com/media/485645/001126.pdf", 3_000_000)]
        texts = {"http://www.apax.com/media/485645/001126.pdf": "Sustainability Report 2013 (PDF, 3 MB)"}
        out = W.candidates("PT_00117", "Apax", rows, set(), 2026, link_text=texts)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["source_type"], "Sustainability Report")
        self.assertEqual(out[0]["match_confidence"], "wayback (link text)")
        self.assertEqual(out[0]["doc_year"], "2013")
        self.assertIn("Link text on the archived page: Sustainability Report 2013", out[0]["Reviewer notes"])

    def test_text_type(self):
        self.assertEqual(W.text_type("Annual Report 2012"), "Annual Report")
        self.assertEqual(W.text_type("ESG Report"), "ESG Report")
        self.assertEqual(W.text_type("Download"), "")
        self.assertEqual(W.text_type("Privacy policy"), "")

    def test_archived_reports_page_is_read_with_the_harvester_extraction(self):
        html = ('<html><body><a href="/media/1/001125.pdf">Sustainability Report 2014</a>'
                '<a href="/about">About us</a><a href="/media/2/q1.pdf">Q1 results</a></body></html>')
        class R:
            def __init__(self, status, body=None, text="", ctype="text/html"):
                self.status_code, self._b, self.text, self.headers = status, body, text, {"Content-Type": ctype}
            def json(self):
                return self._b
        sess = mock.Mock()
        sess.get.side_effect = [R(200, [["timestamp"], ["20150101000000"]]), R(200, text=html)]
        with mock.patch.object(W.time, "sleep"):
            got = W.hub_links("https://www.apax.com/reports", sess)
        self.assertEqual(got, {"https://www.apax.com/media/1/001125.pdf": ("Sustainability Report 2014", "20150101000000"),
                               "https://www.apax.com/media/2/q1.pdf": ("Q1 results", "20150101000000")})
        self.assertEqual(sess.get.call_args_list[1].args[0],
                         "https://web.archive.org/web/20150101000000id_/https://www.apax.com/reports")


class Sites(unittest.TestCase):
    ROWS = [{"fields": {"wba_id": "PT_01912", "url": "https://www.sib.wa.gov/financial/reports.html", "type": "reports_hub"}},
            {"fields": {"wba_id": "PT_01912", "url": "https://www.sib.wa.gov/about", "type": "other"}},
            {"fields": {"wba_id": "PT_01912", "url": "https://lobbymap.org/company/x", "type": "other"}},
            {"fields": {"wba_id": "PT_00117", "url": "https://www.apax.com/responsibility/", "type": "sustainability_page"}},
            {"fields": {"wba_id": "PT_00117", "url": "https://www.apax.com/reports/", "type": "reports_hub"}},
            {"fields": {"wba_id": "PT_00117", "url": "https://esgbook.com/apax", "type": "other"}}]
    OWNERS = {"sib.wa.gov": {"PT_01912"}, "lobbymap.org": {"A", "B", "C", "PT_01912"}, "apax.com": {"PT_00117"},
              "esgbook.com": {"PT_00117"}}

    def test_hosts_not_registered_domains_and_shared_hosts_left_out(self):
        hosts, hubs = W.sites_from(self.ROWS, {"PT_01912", "PT_00117"}, self.OWNERS)
        self.assertEqual(hosts, {"PT_01912": ["sib.wa.gov"], "PT_00117": ["apax.com"]})   # one page on esgbook.com: a listing
        self.assertEqual(hubs, {"PT_01912": ["https://www.sib.wa.gov/financial/reports.html"],
                                "PT_00117": ["https://www.apax.com/reports/"]})

    def test_website_host_counts_even_with_no_watched_page_and_comes_first(self):
        hosts, _ = W.sites_from(self.ROWS, {"PT_01912", "PT_00117", "PT_09999"}, self.OWNERS,
                                websites={"PT_00117": "apaxglobalalpha.com", "PT_09999": "new.example", "PT_01912": "sib.wa.gov"})
        self.assertEqual(hosts["PT_00117"], ["apaxglobalalpha.com", "apax.com"])
        self.assertEqual(hosts["PT_09999"], ["new.example"])
        self.assertEqual(hosts["PT_01912"], ["sib.wa.gov"])                               # not twice

    def test_at_most_max_domains_and_a_shared_website_host_is_skipped(self):
        rows = [{"fields": {"wba_id": "P", "url": f"https://h{i}.example/p{j}", "type": "other"}} for i in range(5) for j in range(2 + i)]
        hosts, _ = W.sites_from(rows, {"P"}, {f"h{i}.example": {"P"} for i in range(5)}, websites={"P": "sites.google.com"})
        self.assertEqual(hosts["P"], ["h4.example", "h3.example", "h2.example"])


class Selection(unittest.TestCase):
    def formula(self, wbas, everything):
        seen = {}
        def fake(table, fields, formula=None):
            seen["formula"] = formula; seen["fields"] = fields
            return [{"id": "rec1", "fields": {"wba_id": "PT_1", "company_name": "One", "website": "https://www.one.example/x"}}]
        with mock.patch.object(W, "at_get", fake):
            rows = W.gap_companies(wbas, everything)
        self.assertEqual(rows, [("rec1", "PT_1", "One", "one.example")])
        return seen

    def test_named_companies_are_always_looked_at(self):
        seen = self.formula(["PT_1", "PT_2"], False)
        self.assertEqual(seen["formula"], "OR({wba_id}='PT_1',{wba_id}='PT_2')")

    def test_gap_scope_skips_companies_checked_recently(self):
        seen = self.formula([], False)
        self.assertTrue(seen["formula"].startswith("AND(OR({kr_latest_status}='None'"))
        self.assertIn("IS_BEFORE({wayback_checked}, DATEADD(TODAY(), -90, 'days'))", seen["formula"])
        self.assertIn("{wayback_checked}=''", seen["formula"])
        self.assertIn("wayback_checked", seen["fields"])

    def test_all_scope_still_skips_companies_checked_recently(self):
        seen = self.formula([], True)
        self.assertTrue(seen["formula"].startswith("OR({wayback_checked}=''"))
        self.assertNotIn("kr_latest_status", seen["formula"])


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
        params = sess.get.call_args.kwargs["params"]
        self.assertEqual(params["matchType"], "host")
        self.assertEqual(params["filter"], ["mimetype:application/pdf", "statuscode:200"])   # no regex: it only slowed the scan
        self.assertEqual(params["limit"], str(W.CDX_LIMIT))
        sess.get.return_value = R(200, [])
        self.assertEqual(W.cdx_rows("x.com", sess), [])                  # answered: nothing there
        sess.get.return_value = R(404)
        self.assertEqual(W.cdx_rows("x.com", sess), [])

    def test_a_timeout_is_not_tried_again_but_a_slow_down_answer_is(self):
        import requests
        sess = mock.Mock()
        sess.get.side_effect = requests.exceptions.ReadTimeout()
        with mock.patch.object(W.time, "sleep"):
            self.assertIsNone(W.cdx_rows("x.com", sess))                 # no answer, not "nothing": not stamped
        self.assertEqual(sess.get.call_count, 1)
        sess.get.side_effect = [mock.Mock(status_code=503)] * 3
        with mock.patch.object(W.time, "sleep"):
            self.assertIsNone(W.cdx_rows("x.com", sess))
        self.assertEqual(sess.get.call_count, 4)
        ok = mock.Mock(status_code=200); ok.json.return_value = [["t", "o", "l"], ["20150905084903", "http://x/a.pdf", "1"]]
        sess.get.side_effect = [mock.Mock(status_code=503), ok]
        with mock.patch.object(W.time, "sleep"):
            self.assertEqual(W.cdx_rows("x.com", sess), [("20150905084903", "http://x/a.pdf", 1)])
        self.assertEqual(sess.get.call_count, 6)                          # 1 timeout + 3 slow-downs + 503, 200


if __name__ == "__main__":
    unittest.main()
