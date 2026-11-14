import os, unittest
from datetime import datetime, timezone, timedelta

import page_scans as P
import scan_now as S

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQL = open(os.path.join(ROOT, "supabase", "search.sql"), encoding="utf-8").read()
NOW = datetime(2026, 11, 14, 18, 0, tzinfo=timezone.utc)


def link(rec, wba, url, scan_now=False, status=""):
    return {"id": rec, "fields": {"wba_id": wba, "url": url, "scan_now": scan_now, "scan_status": status}}


class Norm(unittest.TestCase):
    def test_one_spelling_for_one_page(self):
        same = ["https://www.acme.com/reports/", "http://acme.com/reports", "https://ACME.com/reports#latest"]
        self.assertEqual({P.norm(u) for u in same}, {"acme.com/reports"})
        self.assertNotEqual(P.norm("https://acme.com/reports?year=2024"), P.norm("https://acme.com/reports"))


class Plan(unittest.TestCase):
    def test_a_watched_page_is_ticked_and_an_unknown_one_is_added(self):
        links = [link("recA", "PT_1", "https://www.acme.com/reports/", status="Scanned 1 Nov: 3 documents")]
        ticks, adds = P.plan([(1, "PT_1", "https://acme.com/reports"), (2, "PT_1", "https://acme.com/esg")], links)
        self.assertEqual(ticks, [(1, "recA", "Scanned 1 Nov: 3 documents")])
        self.assertEqual(adds, [(2, "PT_1", "https://acme.com/esg")])

    def test_the_same_address_under_another_company_is_not_that_companys_page(self):
        ticks, adds = P.plan([(1, "PT_2", "https://acme.com/reports")], [link("recA", "PT_1", "https://acme.com/reports")])
        self.assertEqual((ticks, len(adds)), ([], 1))


class Settle(unittest.TestCase):
    def req(self, rid, status="queued", rec="recA", before="old", minutes=10, url="https://acme.com/reports"):
        return (rid, "PT_1", url, status, rec, before, NOW - timedelta(minutes=minutes))

    def test_done_only_when_the_scan_has_written_a_new_result(self):
        waiting = [link("recA", "PT_1", "https://acme.com/reports", scan_now=True, status="old")]
        running = [link("recA", "PT_1", "https://acme.com/reports", scan_now=True, status="Scanning since 2026-11-14 17:55 UTC ...")]
        unchanged = [link("recA", "PT_1", "https://acme.com/reports", status="old")]
        finished = [link("recA", "PT_1", "https://acme.com/reports", status="Scanned 14 Nov 17:58: 12 documents, 2 new")]
        for links in (waiting, running, unchanged):
            self.assertEqual(P.settle([self.req(1)], links, NOW), ([], [], []))
        self.assertEqual(P.settle([self.req(1)], finished, NOW), ([(1, "Scanned 14 Nov 17:58: 12 documents, 2 new")], [], []))

    def test_an_added_page_is_tied_to_its_row_when_the_row_appears_and_done_when_it_has_been_read(self):
        new_row = [link("recB", "PT_1", "https://acme.com/esg")]
        self.assertEqual(P.settle([self.req(2, "added", None, None, url="https://acme.com/esg")], new_row, NOW), ([], [(2, "recB")], []))
        read = [link("recB", "PT_1", "https://acme.com/esg", status="Scanned 14 Nov: 4 documents, 4 new")]
        self.assertEqual(P.settle([self.req(2, "added", None, None, url="https://acme.com/esg")], read, NOW)[0], [(2, "Scanned 14 Nov: 4 documents, 4 new")])

    def test_no_result_after_three_hours_is_closed_as_failed(self):
        self.assertEqual(P.settle([self.req(3, minutes=181)], [link("recA", "PT_1", "https://acme.com/reports", scan_now=True, status="old")], NOW), ([], [], [3]))
        self.assertEqual(P.settle([self.req(3, "added", None, None, minutes=200)], [], NOW), ([], [], [3]))


class Watch(unittest.TestCase):
    def test_the_watch_runs_it_at_every_check_and_survives_its_failure(self):
        src = open(os.path.join(ROOT, "scan_now.py"), encoding="utf-8").read()
        self.assertIn("            side_jobs()\n        page_jobs()\n", src)
        keep = os.environ.get("SUPABASE_DB_URL")
        try:
            os.environ.pop("SUPABASE_DB_URL", None)
            self.assertIsNone(S.page_jobs(run=lambda *a, **k: self.fail("ran")))
            os.environ["SUPABASE_DB_URL"] = "postgresql://example"

            def boom(*a, **k):
                raise TimeoutError()
            self.assertEqual(S.page_jobs(run=boom), -1)
            seen = []
            S.page_jobs(run=lambda cmd, **k: seen.append(cmd))
            self.assertEqual(seen[0][1:], ["page_scans.py", "--commit"])
        finally:
            if keep is None: os.environ.pop("SUPABASE_DB_URL", None)
            else: os.environ["SUPABASE_DB_URL"] = keep


class Sql(unittest.TestCase):
    def test_asking_is_signed_checked_and_one_open_request_a_page(self):
        src = SQL[SQL.index("create or replace function public.scan_page("):SQL.index("create or replace function public.page_scan_last(")]
        self.assertIn("public.email_may_search(who)", src)
        self.assertIn("p.status in ('new', 'queued', 'added') and p.at > now() - interval '3 hours'", src)
        self.assertIn("'waiting', true", src)
        self.assertIn(">= 60", src)                                            # a person, a day
        self.assertIn("to authenticated;", src); self.assertIn("from public, anon;", src)
        self.assertIn("alter table public.page_scans enable row level security;", SQL)


class AlreadySent(unittest.TestCase):
    def test_a_second_address_of_a_file_already_sent_is_remembered_and_shown_as_held(self):
        begin = SQL[SQL.index("create or replace function public.upload_begin("):SQL.index("create or replace function public.upload_known(")]
        known = SQL[SQL.index("create or replace function public.upload_known("):SQL.index("create or replace function public.upload_done(")]
        self.assertIn("insert into public.browser_upload_urls (wba_id, url, upload_id) values (co, left(src, 1000), row.id) on conflict do nothing;", begin)
        self.assertLess(begin.index("insert into public.browser_upload_urls"), begin.index("return jsonb_build_object('again', true);"))
        self.assertIn("from public.browser_upload_urls a where a.wba_id = co and a.url = x.u", known)
        self.assertIn("alter table public.browser_upload_urls enable row level security;", SQL)
        self.assertLess(SQL.index("create table if not exists public.browser_upload_urls"), SQL.index("create or replace function public.upload_begin("))


class Extension(unittest.TestCase):
    def test_the_button_asks_for_a_scan_and_shows_what_became_of_it(self):
        js = open(os.path.join(ROOT, "extension", "send.js"), encoding="utf-8").read()
        html = open(os.path.join(ROOT, "extension", "send.html"), encoding="utf-8").read()
        self.assertIn('rpc("scan_page", { company: company.id, url: pageUrl(), client: CLIENT })', js)
        self.assertIn('rpc("page_scan_last", { url: pageUrl() })', js)
        self.assertIn("setTimeout(() => showScan(true), 20000)", js)            # follows an open request, 20 seconds apart
        self.assertIn(">Scan this page</button>", html); self.assertIn('id="scanmsg"', html)
        self.assertNotIn("add_sources", js)                                     # the old "Watch this page" route is gone from the extension


if __name__ == "__main__":
    unittest.main()
