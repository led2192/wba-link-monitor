import unittest

import discover_pages as dp


class ClearWinner(unittest.TestCase):
    def test_single_candidate_with_documents(self):
        c = [("reports_hub", "https://a/reports", "why", 14)]
        self.assertEqual(dp.clear_winner(c)[1], "https://a/reports")

    def test_too_few_documents_waits_for_a_person(self):
        self.assertIsNone(dp.clear_winner([("reports_hub", "https://a/r", "why", 2)]))

    def test_close_second_is_ambiguous(self):
        c = [("reports_hub", "https://a/r1", "w", 10), ("reports_hub", "https://a/r2", "w", 7)]
        self.assertIsNone(dp.clear_winner(c))

    def test_twice_the_next_wins(self):
        c = [("reports_hub", "https://a/r2", "w", 5), ("reports_hub", "https://a/r1", "w", 12)]
        self.assertEqual(dp.clear_winner(c)[1], "https://a/r1")

    def test_plan_splits_by_type(self):
        found = [("reports_hub", "https://a/r", "w", 9), ("sustainability_page", "https://a/s1", "w", 4),
                 ("sustainability_page", "https://a/s2", "w", 4)]
        auto, amb = dp.plan(found, ["reports_hub", "sustainability_page"])
        self.assertEqual([x[1] for x in auto], ["https://a/r"])
        self.assertEqual(len(amb), 2)


class Gone(unittest.TestCase):
    def test_is_gone_only_for_monitored_report_pages_with_404_or_410(self):
        self.assertTrue(dp.is_gone({"monitor": True, "status": "dead", "http_status": 404, "type": "reports_hub"}))
        self.assertTrue(dp.is_gone({"monitor": True, "status": "dead", "http_status": 410.0, "type": "policies"}))
        self.assertFalse(dp.is_gone({"monitor": True, "status": "dead", "http_status": 403, "type": "reports_hub"}))
        self.assertFalse(dp.is_gone({"monitor": False, "status": "dead", "http_status": 404, "type": "reports_hub"}))
        self.assertFalse(dp.is_gone({"monitor": True, "status": "dead", "http_status": 404, "type": "news"}))

    def test_notes(self):
        self.assertEqual(dp.gone_note("2026-10-24", 404.0, replacement="https://a/new"),
                         "2026-10-24: gone (404); replaced by https://a/new")
        self.assertIn("another page of this type", dp.gone_note("2026-10-24", 410, other_live=True))
        self.assertIn("no replacement found", dp.gone_note("2026-10-24", 404))


def page(pid, url, typ, monitor=True, status="ok", notes=""):
    return {"id": pid, "fields": {"url": url, "type": typ, "monitor": monitor, "status": status,
                                  "http_status": 404 if status == "dead" else 200, "notes": notes}}


class GoneListings(unittest.TestCase):
    """Real cases of 24-25 Oct 2026: gone report listings unticked without a search."""

    def test_hanwha_listing_is_not_covered_by_a_topic_page(self):
        gone = page("g1", "https://www.hanwha.com/sustainability/reports.do", "sustainability_page", status="dead")
        live = page("p1", "https://www.hanwha.com/sustainability/esg-in-action/social.do", "sustainability_page")
        replace, covered = dp.decide_gone([gone], [gone, live])
        self.assertFalse(covered["g1"])
        self.assertEqual(replace, ["sustainability_page"])

    def test_safaricom_listing_is_covered_by_the_new_listing(self):
        gone = page("g1", "https://www.safaricom.co.ke/investor-relations-category/annual-reports", "reports_hub",
                    status="dead")
        live = page("p1", "https://www.safaricom.co.ke/investor-relations-landing/reports/annual-reports", "reports_hub")
        replace, covered = dp.decide_gone([gone], [gone, live])
        self.assertTrue(covered["g1"])
        self.assertEqual(replace, [])

    def test_a_topic_page_is_still_covered_by_any_live_page_of_its_type(self):
        gone = page("g1", "https://www.glencore.com/sustainability/ethics-and-compliance/programme", "policies",
                    status="dead")
        live = page("p1", "https://www.glencore.com/who-we-are/policies", "policies")
        replace, covered = dp.decide_gone([gone], [gone, live])
        self.assertTrue(covered["g1"])
        self.assertEqual(replace, [])

    def test_unticked_or_dead_listings_cover_nothing(self):
        gone = page("g1", "https://www.dai-ichi-life-hd.com/en/investor/library/annual_report/index.html",
                    "reports_hub", status="dead")
        off = page("p1", "https://www.dai-ichi-life-hd.com/en/sustainability/report/2018/pdf/index_007.pdf",
                   "reports_hub", monitor=False)
        dead = page("p2", "https://www.dai-ichi-life-hd.com/en/investor/library/index.html", "reports_hub",
                    status="dead")
        replace, covered = dp.decide_gone([gone], [gone, off, dead])
        self.assertFalse(covered["g1"])
        self.assertEqual(replace, ["reports_hub"])

    def test_listing_reads_the_path_not_the_host(self):
        self.assertTrue(dp.is_listing("https://www.deheus.com/globalassets/sustainability/csr-documents"))
        self.assertTrue(dp.is_listing("https://sustainability.aboitiz.com/sustainability-at-aboitiz/eesg-data/"))
        self.assertTrue(dp.is_listing("https://www.cfindustries.com/sustainability/downloads"))
        self.assertFalse(dp.is_listing("https://reports.example.com/sustainability/cocoa"))
        self.assertFalse(dp.is_listing("https://www.tescoplc.com/sustainability/cocoa"))

    def test_pages_unticked_before_the_fix_are_searched_once(self):
        note = "2026-10-24: gone (404); " + dp.COVERED_NOTE
        f = page("g1", "https://www.hanwha.com/sustainability/reports.do", "sustainability_page", monitor=False,
                 status="dead", notes=note)["fields"]
        self.assertTrue(dp.is_retired_covered(f))
        f["notes"] = dp.gone_note("2026-10-27", 404, recheck=True) + "\n" + note
        self.assertFalse(dp.is_retired_covered(f))
        topic = page("g2", "https://www.tescoplc.com/sustainability/cocoa", "sustainability_page", monitor=False,
                     status="dead", notes=note)["fields"]
        self.assertFalse(dp.is_retired_covered(topic))
        self.assertFalse(dp.is_gone(f))

    def test_recheck_notes(self):
        self.assertEqual(dp.gone_note("2026-10-27", 404, replacement="https://a/new", recheck=True),
                         "2026-10-27: re-checked, replaced by https://a/new")
        self.assertIn("re-checked", dp.gone_note("2026-10-27", 404, other_live=True, recheck=True))


class GoneRunDryRun(unittest.TestCase):
    """main() end to end on stubs: the Hanwha listing is searched and replaced, the covered topic page is not."""

    def test_main_searches_gone_listing(self):
        import os, sys, tempfile, types
        from unittest import mock
        gone = page("g1", "https://www.hanwha.com/sustainability/reports.do", "sustainability_page", status="dead")
        topic = page("g2", "https://www.hanwha.com/sustainability/old-topic.do", "sustainability_page", status="dead")
        live = page("p1", "https://www.hanwha.com/sustainability/esg-in-action/social.do", "sustainability_page")
        company = {"id": "recC", "fields": {"wba_id": "PT_02855", "company_name": "Hanwha",
                                            "website": "https://www.hanwha.com"}}
        for p in (gone, topic, live):
            p["fields"]["wba_id"] = "PT_02855"

        def fake_get(table, params):
            return [company] if table == "companies" else [gone, topic, live]
        searched = []

        def fake_explore(AS, sess, home, want, have):
            searched.append(list(want))
            return [("sustainability_page", "https://www.hanwha.com/sustainability/library.do", "why", 9)], None
        fake_as = types.ModuleType("active_search"); fake_as.normalize = lambda u: (u.lower().rstrip("/"), u)
        fake_inbox = types.ModuleType("inbox"); fake_inbox.load_page_tombstones = lambda: {}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(sys.modules, {"active_search": fake_as, "inbox": fake_inbox}), \
                mock.patch.object(dp, "TOKEN", "t"), mock.patch.object(dp, "BASE", "b"), \
                mock.patch.object(dp, "get", fake_get), mock.patch.object(dp, "candidates", lambda *a: []), \
                mock.patch.object(dp, "gone_by_company", lambda w=None: {"PT_02855": [gone, topic]}), \
                mock.patch.object(dp, "explore", fake_explore), mock.patch.object(sys, "argv", ["discover_pages.py"]):
            cwd = os.getcwd(); os.chdir(d)
            try:
                dp.main()
                md = open("discover_pages.md").read()
            finally:
                os.chdir(cwd)
        self.assertEqual(searched, [["sustainability_page"]])
        self.assertIn("| gone pages replaced | 1 |", md)
        self.assertIn("| gone pages, company still watched through another page of the type | 1 |", md)


if __name__ == "__main__":
    unittest.main()
