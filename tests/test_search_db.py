"""The search functions on a real Postgres (PG_SEARCH_TEST_URL; the Tests workflow provides one, see tests/pg_search.py).

Until 11 Nov 2026 the tests of supabase/search.sql only read its text. That day a question of three lines typed
into the page built a query the database could not carry and took it down for everyone. These tests run the
functions: what a search returns, in what order, and that the worst a person can type comes back in time.
"""
import os, sys, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pg_search

URL = os.environ.get("PG_SEARCH_TEST_URL")


@unittest.skipUnless(URL, "PG_SEARCH_TEST_URL not set")
class SearchOnPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        cls.conn = psycopg.connect(URL, autocommit=True)
        with psycopg.connect(URL) as c:
            pg_search.install(c)
            pg_search.seed(c)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def find(self, q, company=None, pairs=None):
        cur = self.conn.cursor()
        cur.execute("select set_config('lantern.pairs', %s, false)", ("on" if pairs else "off",))
        cur.execute("select library_id, page, rank, excerpt from public.find_pages('', %s, %s)", (q, company))
        return cur.fetchall()

    def test_install_twice_takes_no_lock_on_the_search_tables(self):
        import psycopg, load_supabase as L
        sql = L.guard_sql(open(os.path.join(pg_search.ROOT, "supabase", "search.sql"), encoding="utf-8").read())
        with psycopg.connect(URL) as c:
            cur = c.cursor(); cur.execute(sql)
            cur.execute("select c.relname from pg_locks l join pg_class c on c.oid = l.relation where l.pid = pg_backend_pid() "
                        "and c.relkind = 'r' and l.mode in ('AccessExclusiveLock', 'ShareLock', 'ShareRowExclusiveLock')")
            self.assertEqual(cur.fetchall(), [])                      # nothing a search has to wait for
            c.commit()
            self.assertEqual(len(L.refresh_derived(c)), 2)

    def test_the_year_a_sender_gave_replaces_airtables_before_the_documents_are_written(self):
        import psycopg, load_supabase as L
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("alter table public.documents add column if not exists year_by text")
            cur.execute("create temp table tmp_docs (like public.documents including defaults) on commit drop")
            cur.execute("insert into tmp_docs (library_id, wba_id, year) values ('s-may', 'PT_MAY', 2022), ('s-dec', 'PT_DEC', 2022), "
                        "('s-unknown', 'PT_NONE', 2022), ('s-nobody', 'PT_MAY', 2022)")
            cur.execute("insert into public.company_fiscal (wba_id, fy_end_month, source) values ('PT_MAY', 5, 'pages'), ('PT_DEC', 12, 'pages') on conflict (wba_id) do update set fy_end_month = excluded.fy_end_month")
            for lid, wba, sha, year in (("s-may", "PT_MAY", "1" * 64, 2024), ("s-dec", "PT_DEC", "2" * 64, 2024), ("s-unknown", "PT_NONE", "3" * 64, 2024)):
                cur.execute("insert into public.browser_uploads (uploader, wba_id, filename, bytes, sha256, path, status, sent_year, library_id) "
                            "values ('t@example.org', %s, 'a.pdf', 5000, %s, %s, 'taken', %s, %s)", (wba, sha, wba + "/" + sha + ".pdf", year, lid))
            n = L.sender_years(cur)
            cur.execute("select library_id, year, year_by from tmp_docs order by library_id")
            got = cur.fetchall()
            c.rollback()
        self.assertEqual(n, 3)
        self.assertEqual(got, [("s-dec", 2024, "sender"),          # a year ending in December: the year printed
                               ("s-may", 2023, "sender"),          # a year ending in May: WBA's reporting year is the one before
                               ("s-nobody", 2022, None),           # nobody said anything: Airtable's year stays
                               ("s-unknown", 2024, "sender")])     # no fiscal month known: the year printed

    def test_a_report_of_one_or_two_pages_is_not_shown_as_a_report(self):
        import psycopg, load_supabase as L
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("alter table public.documents add column if not exists type_by text")
            cur.execute("create temp table tmp_docs (like public.documents including defaults) on commit drop")
            cur.execute("insert into tmp_docs (library_id, doc_type, pages) values ('bio', 'Annual Report', 1), ('note', 'Integrated Report', 2), "
                        "('real', 'Annual Report', 240), ('three', 'Sustainability Report', 3), ('nocount', 'Annual Report', null), "
                        "('zero', 'Annual Report', 0), ('policy', 'Policy Documents', 1)")
            n = L.short_types(cur)
            cur.execute("select library_id, doc_type, type_by from tmp_docs order by library_id")
            got = cur.fetchall()
            c.rollback()
        self.assertEqual(n, 2)
        self.assertEqual(got, [("bio", "UNKNOWN", "too short"), ("nocount", "Annual Report", None), ("note", "UNKNOWN", "too short"),
                               ("policy", "Policy Documents", None),       # a one-page policy is a policy
                               ("real", "Annual Report", None), ("three", "Sustainability Report", None),
                               ("zero", "Annual Report", None)])           # no pages counted: its length is not known

    def test_what_a_companys_traits_say_about_a_missing_report(self):
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("insert into public.company_notes (wba_id, traits) values ('PT_N1', '{no_reports,local_language}'), ('PT_N2', '{sust_in_annual,no_annual}'), "
                        "('PT_N3', '{in_parent}'), ('PT_N4', '{refuses_robot}'), ('PT_N5', '{no_sustainability}')")
            got = []
            for w in ("PT_N1", "PT_N2", "PT_N3", "PT_N4", "PT_N5", "PT_NOBODY"):
                cur.execute("select public.company_implied(%s, 'annual'), public.company_implied(%s, 'sustainability')", (w, w))
                got.append(cur.fetchone())
            cur.execute("select public.company_traits() @> array['no_reports', 'in_parent', 'refuses_robot']")
            listed = cur.fetchone()[0]
            c.rollback()
        self.assertEqual(got, [("not_published", "not_published"),      # publishes almost nothing: neither report is a gap
                               ("not_published", "inside_annual"),      # each kind by its own trait
                               ("parent", "parent"),
                               (None, None),                            # a trait that describes, and explains no gap
                               (None, "not_published"),
                               (None, None)])
        self.assertTrue(listed)

    def test_company_first_finds_the_same_pages(self):
        """Option company_first: four words or more within a company read the company's pages; the list is the same."""
        cur = self.conn.cursor()
        cur.execute("select set_config('lantern.company_first', 'off', false)")
        usual = self.find("zebra scope 3 emissions target", "PT_00000")
        cur.execute("select set_config('lantern.company_first', 'on', false)")
        try:
            first = self.find("zebra scope 3 emissions target", "PT_00000")
            short = self.find("zebra target", "PT_00000")                     # under four words: the word index, as before
        finally:
            cur.execute("select set_config('lantern.company_first', 'off', false)")
        self.assertTrue(usual)
        self.assertEqual([(r[0], r[1]) for r in first], [(r[0], r[1]) for r in usual])
        self.assertEqual(first[0][0], "targetpage")
        self.assertTrue(short)

    def test_words_together_first(self):
        rows = self.find("living wage")
        self.assertTrue(rows)
        self.assertIn("⟦living⟧ ⟦wage⟧", rows[0][3])

    def test_a_quoted_phrase_is_marked_as_one_piece(self):
        rows = self.find('"human rights due diligence"')
        self.assertTrue(rows)
        self.assertTrue(all("⟦human rights due diligence⟧" in r[3] for r in rows))

    def test_the_page_with_every_word_before_the_page_with_some_together(self):
        rows = self.find("zebra scope 3 emissions target", "PT_00000")
        ids = [r[0] for r in rows]
        self.assertEqual(ids[0], "targetpage")                         # the target, not the table of emissions
        self.assertIn("target", rows[0][3])

    def test_pairs_option_finds_the_same_phrase(self):
        off, on = self.find('"living wage"'), self.find('"living wage"', pairs=True)
        self.assertTrue(on)
        self.assertTrue(all("⟦living wage⟧" in r[3] for r in on))
        self.assertTrue(set((r[0], r[1]) for r in on) <= set((r[0], r[1]) for r in off) or len(on) >= 1)

    def test_the_worst_a_person_can_type_comes_back(self):
        # each of these is a search someone made or could make; none may fail or take long on this small library
        cases = [
            "has set intensity targets for its lifecycle emissions (S1&S2 as well as several S3 categories, including S3.11.) "
            "from sold products while reporting only S3 Cat 11 using a methodological change unique to the RY. Is this true?",
            "climate water energy people suppliers workers wage living just transition plan human rights due diligence",
            '"" "a" ((((  &&& ||| !!! ::: *** \\\\ \'\'\' ;;; --',
            "a b c d e f g h i j k l m n o p",
            "'; drop table public.pages; --",
            "the of and to in for on with",
            "x" * 400,
            "2024 2025 2026 1 2 3",
            "  ",
        ]
        for q in cases:
            for company in (None, "PT_00000"):
                for pairs in (False, True):
                    t0 = time.time()
                    try:
                        self.find(q, company, pairs)
                    except Exception as e:
                        if "search words" in str(e) or "type something" in str(e).lower() or getattr(e, "sqlstate", "") in ("22023", "P0001"):
                            continue                                   # a polite refusal of an empty search is an answer
                        raise AssertionError(f"search failed for {q[:40]!r} (company {company}, pairs {pairs}): {e}")
                    self.assertLess(time.time() - t0, 10, q[:40])
        cur = self.conn.cursor(); cur.execute("select count(*) from public.pages")
        self.assertGreater(cur.fetchone()[0], 1000)                    # and the table is still there

    def test_a_search_never_uses_more_than_six_words(self):
        self.find("climate water energy people suppliers workers wage living")
        cur = self.conn.cursor(); cur.execute("select terms from public.search_log order by id desc limit 1")
        self.assertLessEqual(len((cur.fetchone()[0] or "").split()), 6)

    def test_a_company_a_person_adds_exists_at_once(self):
        import add_companies as A
        cur = self.conn.cursor()
        cur.execute("delete from public.company_requests"); cur.execute("delete from public.company_overlaps"); cur.execute("delete from public.companies where wba_id like 'LN\\_%'")
        cur.execute("alter table public.companies add column if not exists lei text")
        # the name as compared and the id are the ones add_companies.py gives
        for name in ("Nestlé S.A.", "Target Corporation", "The Coca-Cola Company", "Grupo Bimbo S.A.B. de C.V.", "PT Astra International Tbk", "Ørsted A/S"):
            cur.execute("select public.name_key(%s)", (name,)); self.assertEqual(cur.fetchone()[0], A.norm_name(name), name)
        cur.execute("select public.site_label('investors.example.co.uk'), public.site_label('example.com'), public.site_label('localhost')")
        self.assertEqual(cur.fetchone(), ("example", "example", ""))
        cur.execute("select public.request_company('Acme Robotics Ltd', 'acme.example.com', null, 'for 2027')"); j = cur.fetchone()[0]
        self.assertTrue(j["created"]); self.assertEqual(j["id"], A.company_id("Acme Robotics Ltd", "")); self.assertEqual(j["doubts"], [])
        cur.execute("select name, website from public.companies where wba_id = %s", (j["id"],)); self.assertEqual(cur.fetchone(), ("Acme Robotics Ltd", "acme.example.com"))
        cur.execute("select public.upload_begin(%s, 'a.pdf', 5000, %s)", (j["id"], "ef" * 32)); self.assertEqual(cur.fetchone()[0]["path"], f"{j['id']}/{'ef' * 32}.pdf")      # its documents at once
        # clear duplicates are not made: the same name without its legal ending, the same website, the same LEI
        cur.execute("select public.request_company('ACME ROBOTICS')"); k = cur.fetchone()[0]; self.assertTrue(k["held"]); self.assertEqual(k["id"], j["id"])
        cur.execute("select public.request_company('Unilever')"); self.assertTrue(cur.fetchone()[0]["held"])
        cur.execute("update public.companies set website = 'https://www.unilever.com/', lei = '549300MKFYEKVRWML317' where name = 'Unilever'")
        cur.execute("select public.request_company('Unilever Global Plc', 'unilever.com/investors')"); k = cur.fetchone()[0]
        self.assertTrue(k["held"]); self.assertEqual(k["name"], "Unilever")
        cur.execute("select public.request_company('Something Else', null, '549300MKFYEKVRWML317')"); self.assertEqual(cur.fetchone()[0]["name"], "Unilever")
        cur.execute("select public.site_host('HTTPS://www.Example.com:443/en/')"); self.assertEqual(cur.fetchone()[0], "example.com")
        # a doubt does not stop the company: it is noted for the owner
        cur.execute("select public.request_company('Unilever Nigeria', 'unilever.com.ng')"); d = cur.fetchone()[0]
        self.assertTrue(d["created"]); self.assertTrue(any("Unilever" in x for x in d["doubts"])); self.assertTrue(any("website" in x for x in d["doubts"]))
        cur.execute("select public.request_company('Zeta Mining')"); self.assertEqual(cur.fetchone()[0]["doubts"], ["no website given: its pages cannot be searched for"])
        # a document of the new company that another company already holds is noted, by its address and by the file
        cur.execute("insert into public.documents (library_id, wba_id, company, url, pages) values ('ovl-1', 'PT_00000', 'Other', 'https://example.com/shared.pdf', 3) on conflict do nothing")
        cur.execute("select public.upload_known(%s, array['https://example.com/shared.pdf', 'https://example.com/new.pdf'])", (d["id"],)); self.assertEqual(cur.fetchone()[0], ["https://example.com/shared.pdf"])
        cur.execute("delete from public.browser_uploads where sha256 = %s", ("cd" * 32,))
        cur.execute("select public.upload_begin('PT_00000', 'b.pdf', 5000, %s)", ("cd" * 32,)); up = cur.fetchone()[0]["id"]; cur.execute("select public.upload_done(%s)", (up,))
        cur.execute("select public.upload_begin(%s, 'b.pdf', 5000, %s)", (d["id"], "cd" * 32))
        cur.execute("select public.admin_companies('k')"); rows = {c["name"]: c for c in cur.fetchone()[0]}
        self.assertEqual(rows["Unilever Nigeria"]["overlaps"], [{"id": "PT_00000", "name": rows["Unilever Nigeria"]["overlaps"][0]["name"], "n": 2}])
        self.assertEqual((rows["Acme Robotics Ltd"]["files"], rows["Acme Robotics Ltd"]["review"]), (0, None))      # registered, not stored yet
        # the owner accepts one and rejects the other: the rejected one leaves the list and cannot be made again
        cur.execute("select public.admin_company_set('k', %s, 'accepted')", (rows["Acme Robotics Ltd"]["request"],)); self.assertTrue(cur.fetchone()[0]["ok"])
        cur.execute("select public.admin_company_set('k', %s, 'rejected', 'a unit of Unilever')", (rows["Unilever Nigeria"]["request"],)); self.assertTrue(cur.fetchone()[0]["ok"])
        cur.execute("select count(*) from public.companies where wba_id = %s", (d["id"],)); self.assertEqual(cur.fetchone()[0], 0)
        cur.execute("select status from public.browser_uploads where wba_id = %s", (d["id"],)); self.assertEqual(cur.fetchall(), [("orphan",)])
        cur.execute("select public.request_company('Unilever Nigeria', 'unilever.com.ng')"); again = cur.fetchone()[0]
        self.assertEqual((again["again"], again["review"], again["why"]), (True, "rejected", "a unit of Unilever"))
        cur.execute("select count(*) from public.companies where wba_id = %s", (j["id"],)); self.assertEqual(cur.fetchone()[0], 1)
        # the rejection is undone: the company is back and its file waits again; then it is merged into another company
        cur.execute("select public.admin_company_set('k', %s, 'reopened')", (rows["Unilever Nigeria"]["request"],)); self.assertTrue(cur.fetchone()[0]["ok"])
        cur.execute("select count(*) from public.companies where wba_id = %s", (d["id"],)); self.assertEqual(cur.fetchone()[0], 1)
        cur.execute("update public.browser_uploads set status = 'taken', library_id = 'x' where wba_id = %s", (d["id"],))
        cur.execute("select public.upload_begin(%s, 'c.pdf', 6000, %s)", (d["id"], "ab" * 31 + "cd")); cur.execute("update public.browser_uploads set status = 'taken' where sha256 = %s", ("ab" * 31 + "cd",))
        cur.execute("select public.admin_company_set('k', %s, 'merged', null, 'PT_00000')", (rows["Unilever Nigeria"]["request"],)); m = cur.fetchone()[0]; self.assertTrue(m["ok"])
        cur.execute("select wba_id, status, library_id from public.browser_uploads where sha256 in (%s, %s) and wba_id in ('PT_00000', %s) order by bytes, wba_id", ("cd" * 32, "ab" * 31 + "cd", d["id"]))
        got = cur.fetchall()
        self.assertIn(("PT_00000", "stored", None), got)                                     # the file only it had is sent again, for the other company
        self.assertIn((d["id"], "known", "x"), got)                                           # the file both had stays, known
        cur.execute("select review, merged_into from public.company_requests where id = %s", (rows["Unilever Nigeria"]["request"],)); self.assertEqual(cur.fetchone(), ("rejected", "PT_00000"))
        cur.execute("select public.admin_company_set('k', %s, 'reopened')", (rows["Unilever Nigeria"]["request"],)); self.assertFalse(cur.fetchone()[0]["ok"])      # a merge is not undone
        # the owner corrects a company people added; another company's name or LEI is refused; a company of WBA's list is not edited here
        cur.execute("alter table public.companies add column if not exists isin text")
        cur.execute("select public.admin_company_edit('k', %s, %s)", (j["id"], '{"name": "Acme Robotics Group", "lei": "529900t8bm49aursdo55", "isin": "ES0109067019", "industry": "Capital Goods", "country": "esp"}'))
        self.assertTrue(cur.fetchone()[0]["ok"])
        cur.execute("select name, lei, isin, industry, country from public.companies where wba_id = %s", (j["id"],))
        self.assertEqual(cur.fetchone(), ("Acme Robotics Group", "529900T8BM49AURSDO55", "ES0109067019", "Capital Goods", "ESP"))
        cur.execute("select fields ->> 'country', applied from public.company_edits where wba_id = %s", (j["id"],)); self.assertEqual(cur.fetchall(), [("ESP", None)])
        cur.execute("select public.admin_company_edit('k', %s, %s)", (j["id"], '{"name": "Unilever PLC"}')); self.assertIn("Unilever", cur.fetchone()[0]["why"])
        for co, bad in ((j["id"], '{"lei": "123"}'), (j["id"], '{"wba_id": "X"}'), ("PT_00000", '{"name": "Other"}')):
            with self.assertRaises(Exception):
                cur.execute("select public.admin_company_edit('k', %s, %s)", (co, bad))
        with self.assertRaises(Exception):
            cur.execute("select public.admin_company_set('k', 999999, 'accepted')")
        for bad in (("X",), ("Beta", "not a site"), ("Gamma", None, "123")):
            with self.assertRaises(Exception):
                cur.execute("select public.request_company(%s, %s, %s)", (bad + (None, None))[:3])
        cur.execute("delete from public.companies where wba_id like 'LN\\_%'"); cur.execute("delete from public.browser_uploads where wba_id like 'LN\\_%%' or sha256 in (%s, %s)", ("cd" * 32, "ab" * 31 + "cd")); cur.execute("delete from public.company_edits")

    def test_a_file_sent_from_the_browser_is_registered_once(self):
        cur = self.conn.cursor()
        cur.execute("delete from public.browser_uploads")
        sha = "ab" * 32
        cur.execute("select public.upload_begin('PT_00000', 'Annual report 2024.pdf', 5000000, %s, 'https://example.com/ar-2024.pdf', 'https://example.com/reports')", (sha,))
        j = cur.fetchone()[0]; self.assertEqual(j["path"], f"PT_00000/{sha}.pdf")
        cur.execute("select public.upload_expected(%s)", (j["path"],)); self.assertTrue(cur.fetchone()[0])          # the storage rule lets this object in
        cur.execute("select public.upload_expected('PT_00000/other.pdf')"); self.assertFalse(cur.fetchone()[0])      # and no other
        cur.execute("select public.upload_begin('PT_00000', 'x.pdf', 5000000, %s)", (sha,)); self.assertEqual(cur.fetchone()[0]["id"], j["id"])   # not stored yet: the same place again
        cur.execute("select public.upload_done(%s)", (j["id"],)); self.assertTrue(cur.fetchone()[0]["stored"])
        cur.execute("select public.upload_done(%s)", (j["id"],)); self.assertFalse(cur.fetchone()[0]["stored"])
        cur.execute("select public.upload_expected(%s)", (j["path"],)); self.assertFalse(cur.fetchone()[0])         # stored: the place is closed
        cur.execute("select public.upload_begin('PT_00000', 'x.pdf', 5000000, %s)", (sha,)); self.assertTrue(cur.fetchone()[0]["again"])
        cur.execute("select status, filename, source_url from public.browser_uploads"); self.assertEqual(cur.fetchall(), [("stored", "Annual report 2024.pdf", "https://example.com/ar-2024.pdf")])
        # what the sender says of the document (extension 0.4.0): kept with the upload; only a type of the library and a plausible year
        cur.execute("select public.upload_begin('PT_00000', 'from disk.pdf', 5000, %s, null, 'https://example.com/reports', 'send-to-lantern 0.4.0', 'Annual Report', 2024)", ("cd" * 32,))
        self.assertIn("path", cur.fetchone()[0])
        cur.execute("select public.upload_begin('PT_00000', 'from disk.pdf', 5000, %s, sent_year => 2023)", ("cd" * 32,))        # said again before storing: the last word stands, the type is kept
        cur.execute("select sent_type, sent_year, source_url from public.browser_uploads where sha256 = %s", ("cd" * 32,)); self.assertEqual(cur.fetchall(), [("Annual Report", 2023, None)])
        import psycopg
        for bad_type, bad_year in (("Brochure", None), (None, 1066)):
            with self.assertRaises(psycopg.Error):
                cur.execute("select public.upload_begin('PT_00000', 'y.pdf', 5000, %s, sent_type => %s, sent_year => %s)", ("ee" * 32, bad_type, bad_year))
        for bad in (("PT_nope", "a.pdf", 5000, sha, None), ("PT_00000", "a.pdf", 10, sha, None), ("PT_00000", "a.pdf", 210000000, sha, None),
                    ("PT_00000", "a.pdf", 5000, "zz", None), ("PT_00000", "a.pdf", 5000, "cd" * 32, "not a link")):
            with self.assertRaises(Exception):
                cur.execute("select public.upload_begin(%s, %s, %s, %s, %s)", bad)
        cur.execute("select jsonb_array_length(public.upload_companies()) > 0"); self.assertTrue(cur.fetchone()[0])
        cur.execute("select public.upload_known('PT_00000', %s)", (["https://example.com/ar-2024.pdf", "https://example.com/never.pdf"],))
        self.assertEqual(cur.fetchone()[0], ["https://example.com/ar-2024.pdf"])                                    # sent before: not offered again

    def test_documents_known_and_not_held_are_listed_for_their_company(self):
        cur = self.conn.cursor()
        cur.execute("delete from public.unheld_documents")
        cur.execute("""insert into public.unheld_documents (library_id, wba_id, url, label, doc_type, status) values
                       ('PT_00000-x1', 'PT_00000', 'https://example.com/ar-2024.pdf', '2024 | Unilever | Annual Report', 'Annual Report', 'blocked'),
                       ('PT_00000-x2', 'PT_00000', 'https://example.com/old.pdf', '2019 | Unilever | Old', null, 'dead'),
                       ('PT_99999-y', 'PT_99999', 'https://other.example/a.pdf', 'Other', null, 'blocked')""")
        cur.execute("select public.unheld_documents('PT_00000')"); j = cur.fetchone()[0]
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "site", "search.html"), encoding="utf-8").read()
        self.assertIn('/rest/v1/rpc/unheld_documents', src); self.assertIn("JSON.stringify({ company: co, key })", src)   # the page asks with its key, as it searches
        self.assertNotIn('rpc("unheld_documents"', src)                                      # and not through rpc(), which forgets the key when refused
        self.assertEqual((j["n"], j["blocked"]), (2, 1))
        self.assertEqual([d["url"] for d in j["documents"]], ["https://example.com/ar-2024.pdf", "https://example.com/old.pdf"])   # newest label first

    def test_a_load_writes_only_the_documents_that_changed(self):
        import load_supabase as LS, psycopg
        cur = self.conn.cursor()
        conn = psycopg.connect(os.environ["PG_SEARCH_TEST_URL"])        # the loader's own kind of connection: a transaction per call
        self.addCleanup(conn.close)
        row = ("PT_00000-loadtest", "recX", "PT_00000", "Unilever", "Annual Report", 2024, "en", "2024 | Unilever | Test", "https://example.com/t.pdf", 10, 1000, "ok", "p1", "2026-11-12", None, "2026-11-01")
        self.assertEqual(LS.upsert_documents(conn, [row]), 1)
        cur.execute("select loaded_at from public.documents where library_id = 'PT_00000-loadtest'"); first = cur.fetchone()[0]
        self.assertEqual(LS.upsert_documents(conn, [row]), 0)                                  # the same again: nothing is written
        cur.execute("select loaded_at from public.documents where library_id = 'PT_00000-loadtest'"); self.assertEqual(cur.fetchone()[0], first)
        self.assertEqual(LS.upsert_documents(conn, [row[:9] + (12,) + row[10:]]), 1)           # one field differs: written
        cur.execute("select pages from public.documents where library_id = 'PT_00000-loadtest'"); self.assertEqual(cur.fetchone()[0], 12)
        many = [(f"PT_00000-batch{i}",) + row[1:] for i in range(7)]
        said = []
        self.assertEqual(LS.upsert_documents(conn, many, batch=3, say=lambda *a, **k: said.append(a[0])), 7)   # three batches, each committed
        self.assertIn("7 of 7 looked at, 7 written", said[-1])
        self.assertEqual(LS.upsert_documents(conn, many, batch=3, say=lambda *a, **k: None), 0)
        cur.execute("delete from public.documents where library_id = 'PT_00000-loadtest' or library_id like 'PT_00000-batch%'")

    def test_one_search_blends_words_and_meaning(self):
        """find_blend: the words alone without a vector, in quotes or with a place filter; else both lists merged by
        reciprocal rank. The search by meaning is replaced by a stub here (no vector type in the test database)."""
        vec = [0.0] * 384
        import psycopg, psycopg.sql
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("select library_id, page, rank from public.find_pages('', 'living wage')")
            words = cur.fetchall()
            self.assertTrue(words)
            cur.execute("select library_id, page, rank from public.find_blend('', 'living wage')")
            self.assertEqual(cur.fetchall(), words)                         # no vector: the words' answer
            cur.execute("select library_id, page, rank from public.find_blend('', '\"living wage\"', %s::real[])", (vec,))
            self.assertTrue(cur.fetchall())                                  # a phrase in quotes never asks the meaning (which cannot run here)
            last = words[-1]
            cur.execute("select library_id, page from public.pages order by library_id, page")
            other = next(r for r in cur.fetchall() if (r[0], r[1]) not in {(w[0], w[1]) for w in words})
            cur.execute(psycopg.sql.SQL("""create or replace function public.find_meaning(key text, qvec real[], company_filter text default null, year_from int default null, year_to int default null, type_filter text default null, max_rows int default 50)
                           returns table (log_id bigint, rank int, library_id text, company text, wba_id text, year int, doc_type text, label text, url text, page int, score real, excerpt text)
                           language sql as $f$
                             select null::bigint, 1, {}::text, 'c'::text, 'w'::text, 2024, 't'::text, 'l'::text, null::text, {}::int, 0.9::real, 'nearest passage'::text
                             union all
                             select null::bigint, 2, {}::text, 'c'::text, 'w'::text, 2024, 't'::text, 'l'::text, null::text, {}::int, 0.8::real, 'another passage'::text $f$""").format(*[psycopg.sql.Literal(v) for v in (last[0], last[1], other[0], other[1])]))
            cur.execute("select library_id, page, rank, excerpt, log_id from public.find_blend('', 'living wage', %s::real[])", (vec,))
            rows = cur.fetchall()
            c.rollback()                                                     # the stub goes
        keys = [(r[0], r[1]) for r in rows]
        self.assertEqual(len(keys), len(set(keys)))                          # a page both found is one row
        self.assertEqual([r[2] for r in rows], list(range(1, len(rows) + 1)))
        self.assertIn((other[0], other[1]), keys)                            # a page only the meaning found enters
        self.assertEqual(next(r[3] for r in rows if (r[0], r[1]) == (other[0], other[1])), "another passage")
        if len(words) > 1:
            self.assertEqual(keys[0], (last[0], last[1]))                    # found by both: first, though last by words
        self.assertNotEqual(next(r[3] for r in rows if (r[0], r[1]) == (last[0], last[1])), "nearest passage")   # the words' excerpt is kept
        self.assertTrue(all(r[4] is not None for r in rows))                 # the search was logged once, by the words
        self.assertLessEqual(max(sum(1 for k in keys if k[0] == d) for d in {k[0] for k in keys}), 3)
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "site", "search.html"), encoding="utf-8").read()
        self.assertIn('rpc("find_meaning"', src); self.assertIn("function blend(words, meaning, need)", src)   # the page merges the two itself, asked at once
        self.assertNotIn("Search by meaning (beta)", src)                    # one search, not a choice

    def test_the_facts_of_an_answer_are_taken_apart_and_kept_with_their_page(self):
        import json, psycopg
        reply = ('Yes. Nike has a combined target [1].\nFACTS: ```json\n'
                 '[{"what":"Scope 1 emissions","kind":"number","value":"60836","unit":"tCO2e","year":2024,"period":"FY24","source":1},'
                 '{"what":"Scope 1 and 2 target","kind":"yes_no","answer":"yes","source":2},'
                 '{"what":"no page behind it","kind":"text","answer":"x","source":9},'
                 '{"what":"not a number","kind":"number","value":"n/a","source":1},'
                 '{"what":"a year that is none","kind":"text","answer":"y","year":"FY24","source":1}]\n```')
        sources = [{"n": 1, "company": "Nike", "wba_id": "PT_1", "library_id": "L1", "page": 23, "url": "https://x/y.pdf"},
                   {"n": 2, "company": "Nike", "wba_id": "PT_1", "library_id": "L2", "page": 10, "url": None}]
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("select public.ask_split(%s)", (reply,))
            parts = cur.fetchone()[0]
            self.assertEqual(parts["answer"], "Yes. Nike has a combined target [1].")       # the person never sees the list
            self.assertEqual(len(parts["facts"]), 5)
            cur.execute("select public.ask_split('Only an answer.'), public.ask_split(E'A.\nFACTS: not json')")
            plain, broken = cur.fetchone()
            self.assertEqual((plain["answer"], plain["facts"], broken["answer"], broken["facts"]), ("Only an answer.", [], "A.", []))
            cur.execute("select public.ask_keep_facts(7, %s::jsonb, %s::jsonb)", (json.dumps(parts["facts"]), json.dumps(sources)))
            kept = cur.fetchone()[0]
            loose = [{"what": "Net zero target year", "kind": "number", "value": "2,040", "source": "[2]"},
                     {"what": "Net zero target", "kind": "yes_no", "value": "yes", "source": [1, 2]},
                     {"what": "Target scope", "kind": "number", "value": "n/a", "answer": "Scopes 1, 2 and 3", "source": "1"}]
            cur.execute("select public.ask_keep_facts(8, %s::jsonb, %s::jsonb)", (json.dumps(loose), json.dumps(sources)))
            tolerant = cur.fetchone()[0]
            cur.execute("select public.ask_split(%s)", ('A.\nFACTS: [{"what":"x","kind":"text","answer":"y","source":1}]\nThat is all.',))
            trailing = cur.fetchone()[0]
            c.rollback()
        self.assertEqual([(k["kind"], k["value"], k["answer"], k["page"]) for k in tolerant],
                         [("number", 2040, None, 10), ("yes_no", None, "yes", 23), ("text", None, "Scopes 1, 2 and 3", 23)])   # the model's small liberties are read
        self.assertEqual((trailing["stated"], len(trailing["facts"])), (1, 1))                                                  # words after the list do not lose it
        self.assertEqual([k["what"] for k in kept], ["Scope 1 emissions", "Scope 1 and 2 target", "a year that is none"])   # no page, or no figure: not kept
        self.assertEqual((kept[0]["value"], kept[0]["unit"], kept[0]["year"], kept[0]["period"], kept[0]["page"], kept[0]["library_id"]), (60836, "tCO2e", 2024, "FY24", 23, "L1"))
        self.assertEqual((kept[1]["kind"], kept[1]["answer"], kept[1]["verdict"]), ("yes_no", "yes", None))
        self.assertIn("doc_year", kept[0]); self.assertIn("doc_type", kept[0])        # the report the fact was read in travels with it
        self.assertIsNone(kept[2]["year"])
        self.assertNotIn("verdict_by", kept[0])

    def test_a_querys_vector_is_kept_once_a_text(self):
        """query_vector_made with the service replaced by a stub (no http extension here): the reply is read, kept, and
        the second call for the same words is answered from the table."""
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("create schema if not exists vault")
            cur.execute("create table if not exists vault.decrypted_secrets (name text, decrypted_secret text)")
            cur.execute("insert into vault.decrypted_secrets values ('hf_token', 'tok')")
            cur.execute("create schema fakehttp")
            cur.execute("create type fakehttp.http_header as (field text, value text)")
            cur.execute("create type fakehttp.http_request as (method text, uri text, headers fakehttp.http_header[], content_type text, content text)")
            cur.execute("create function fakehttp.http_header(text, text) returns fakehttp.http_header language sql as $f$ select row($1, $2)::fakehttp.http_header $f$")
            cur.execute("create function fakehttp.http_set_curlopt(text, text) returns boolean language sql as $f$ select true $f$")
            cur.execute("""create function fakehttp.http(r fakehttp.http_request) returns table (status int, content text) language sql as
                           $f$ select 200, (select jsonb_build_array(jsonb_agg(g::real / 1000))::text from generate_series(1, 384) g) $f$""")
            cur.execute("insert into pg_extension (oid, extname, extowner, extnamespace, extrelocatable, extversion) select 999999, 'http', 10, n.oid, false, '0' from pg_namespace n where n.nspname = 'fakehttp'")
            cur.execute("select public.query_vector_made('  how   many people ')")
            first = cur.fetchone()[0]
            cur.execute("select public.query_vector_made('how many people')")
            second = cur.fetchone()[0]
            cur.execute("select count(*) from public.query_vectors where q = 'how many people'")
            n = cur.fetchone()[0]
            c.rollback()
        self.assertEqual(len(first["vec"]), 384); self.assertNotIn("held", first)
        self.assertAlmostEqual(first["vec"][1], 0.002, places=5)                # in the service's order
        self.assertTrue(second.get("held")); self.assertEqual(second["vec"], first["vec"]); self.assertEqual(n, 1)

    def test_a_confirmed_fact_serves_the_next_answer_about_the_company(self):
        import json, psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            rows = [(1, "PT_1", "Net zero target", "yes_no", None, "yes", "right", "a@x.org", "2 hours"),
                    (2, "PT_1", "net zero target", "yes_no", None, "yes", "right", "b@x.org", "1 hour"),       # the same fact again: once
                    (3, "PT_1", "Scope 1 emissions", "number", 60836, None, None, None, "1 hour"),               # nobody confirmed it
                    (4, "PT_1", "Employees", "number", 100, None, "wrong", "a@x.org", "1 hour"),                 # marked wrong
                    (5, "PT_2", "Revenue", "number", 5, None, "right", "a@x.org", "1 hour"),                     # another company
                    (6, "PT_1", "Reduction achieved", "number", 69, None, "right", "a@x.org", "5 hours"),     # one figure under three names and two signs: once
                    (7, "PT_1", "Absolute reduction vs FY20", "number", -69, None, "right", "a@x.org", "4 hours"),
                    (8, "PT_1", "Scope 1 and 2 reduction", "number", 69, None, "right", "a@x.org", "3 hours"),
                    (9, "PT_1", "Asked now", "text", None, "x", "right", "a@x.org", "1 minute")]                 # this very answer
            for ask_id, wba, what, kind, value, answer, verdict, by, ago in rows:
                cur.execute("insert into public.ask_facts (ask_id, wba_id, company, what, kind, value, answer, verdict, verdict_by, verdict_at) "
                            "values (%s, %s, 'C', %s, %s, %s, %s, %s, %s, now() - %s::interval)", (ask_id, wba, what, kind, value, answer, verdict, by, ago))
            cur.execute("select public.ask_confirmed_facts(9, %s::jsonb, 'B@x.org')", (json.dumps([{"n": 1, "wba_id": "PT_1"}]),))
            got = cur.fetchone()[0]
            cur.execute("select public.ask_confirmed_lines(%s::jsonb), public.ask_confirmed_lines('[]'::jsonb)", (json.dumps(
                [{"company": "Nike", "what": "Scope 1 emissions", "period": "FY24", "year": 2023, "kind": "number", "value": 60836, "unit": "tCO2e", "page": 23},
                 {"company": "Nike", "what": "Scope 1 and 2 target", "kind": "yes_no", "answer": "yes", "page": 10}]),))
            lines, none = cur.fetchone()
            c.rollback()
        self.assertEqual(lines, "- Nike, Scope 1 emissions FY24: 60836 tCO2e (page 23)\n- Nike, Scope 1 and 2 target: yes (page 10)")
        self.assertEqual(none, "")
        self.assertEqual([(g["what"], g["mine"]) for g in got], [("net zero target", True), ("Scope 1 and 2 reduction", False)])
        self.assertNotIn("verdict_by", got[0])                                     # who confirmed is not sent

    def test_a_fact_gets_its_metric_by_name_and_the_owner_sees_the_taxonomy(self):
        import json, psycopg, load_supabase as L
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("select public.fact_key('Total Scope 1 emissions (2023)'), public.fact_key('Emissions, Scope 1'), public.fact_key('FY24 Número Employees')")
            self.assertEqual(cur.fetchone(), ("1 emission scope", "1 emission scope", "employee numero"))
            cur.execute("select public.fact_match('Direct GHG emissions (Scope 1)'), public.fact_match('Greenhouse gas emissions, Scope 1'), public.fact_match('Net sales 2024'), "
                        "public.fact_match('Scope 2 emissions (market-based)'), public.fact_match('Scope 2 emissions'), public.fact_match('Number of pandas'), public.fact_match('2024')")
            self.assertEqual(cur.fetchone(), ("scope_1", "scope_1", "revenue", "scope_2_market", "scope_2", None, None))
            cur.execute("select count(*), count(distinct grp) from public.fact_metrics")
            metrics, groups = cur.fetchone()
            self.assertGreaterEqual(metrics, 80); self.assertEqual(groups, 24)
            # no two metrics answer to the same name: a name would then go to whichever came first
            cur.execute("select k, count(distinct key) from (select m.key, public.fact_key(n) as k from public.fact_metrics m, unnest(m.aliases || m.label) n) x group by k having count(distinct key) > 1")
            self.assertEqual(cur.fetchall(), [])
            sources = [{"n": 1, "wba_id": "PT_TAX", "company": "Taxonomy Co", "library_id": "tax-1", "page": 4, "url": "https://example.org/a.pdf", "year": 2024, "doc_type": "Annual Report"}]
            facts = [{"what": "Scope 1 GHG emissions", "kind": "number", "value": "120", "unit": "tCO2e", "year": 2024, "source": 1},
                     {"what": "Pandas adopted", "kind": "number", "value": "3", "year": 2024, "source": 1}]
            cur.execute("select public.ask_keep_facts(901, %s::jsonb, %s::jsonb)", (json.dumps(facts), json.dumps(sources)))
            kept = cur.fetchone()[0]
            self.assertEqual([k["metric"] for k in kept], ["scope_1", None])
            # the owner renames a metric and gives it the panda name; installing the file again keeps both and classifies the waiting fact
            cur.execute("update public.fact_metrics set label = 'Sites near protected areas', aliases = aliases || 'Pandas adopted'::text where key = 'sites_near_protected_areas'")
            cur.execute(L.guard_sql(open(os.path.join(pg_search.ROOT, "supabase", "search.sql"), encoding="utf-8").read()))
            cur.execute("select label from public.fact_metrics where key = 'sites_near_protected_areas'")
            self.assertEqual(cur.fetchone()[0], "Sites near protected areas")
            cur.execute("select metric from public.ask_facts where ask_id = 901 order by id")
            self.assertEqual([r[0] for r in cur.fetchall()], ["scope_1", "sites_near_protected_areas"])
            cur.execute("insert into public.ask_facts (ask_id, wba_id, company, what, kind, answer) values (902, 'PT_TAX', 'Taxonomy Co', 'Favourite colour', 'text', 'blue'), (903, 'PT_TAX2', 'Other Co', 'Favourite colours 2023', 'text', 'red')")
            cur.execute("select public.admin_taxonomy('k')")
            t = cur.fetchone()[0]
            self.assertEqual([b["key"] for b in t["blocks"]], ["company", "finance", "sustainability", "commitments"])
            e1 = [g for b in t["blocks"] for g in b["groups"] if g["key"] == "e1"][0]
            self.assertEqual(e1["standard"], "ESRS E1")
            s1 = [m for m in e1["metrics"] if m["key"] == "scope_1"][0]
            self.assertGreaterEqual(s1["facts"], 1); self.assertGreaterEqual(s1["companies"], 1)
            colour = [u for u in t["unclassified"] if u["name"].startswith("Favourite")]
            self.assertEqual([(u["facts"], u["companies"]) for u in colour], [(2, 2)])      # two spellings of one name, counted together
            self.assertEqual(t["facts"] - t["classified"], sum(u["facts"] for u in t["unclassified"]))
            c.rollback()

    def test_the_owner_changes_the_taxonomy_and_every_change_is_logged(self):
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("insert into public.ask_facts (ask_id, wba_id, company, what, kind, value) values (911, 'PT_A', 'A Co', 'Plastic packaging used', 'number', 5), "
                        "(912, 'PT_B', 'B Co', 'Plastic packaging used 2023', 'number', 7), (913, 'PT_A', 'A Co', 'Virgin plastic', 'number', 2), (914, 'PT_A', 'A Co', 'Scope 1 emissions', 'number', 9)")
            cur.execute("select public.fact_rematch()")
            # a new metric from an unclassified name takes every fact with that name
            cur.execute("select public.admin_metric_new('k', 'Plastic packaging used', 'e5', 'number')")
            made = cur.fetchone()[0]
            self.assertEqual((made["ok"], made["key"], made["matched"]), (True, "plastic_packaging_used", 2))
            cur.execute("select public.admin_metric_new('k', 'Used plastic packaging 2024', 'e5') ->> 'error', public.admin_metric_new('k', 'X', 'nowhere') ->> 'error', public.admin_metric_new('k', ' ', 'e5') ->> 'error'")
            self.assertEqual(cur.fetchone(), ('"Used plastic packaging 2024" is already a name of "Plastic packaging used"', "no such group", "a name is needed"))
            # an unclassified name becomes another name of a metric; a name another metric answers to is refused
            cur.execute("select public.admin_metric_edit('k', 'plastic_packaging_used', %s::jsonb)", ('{"aliases": ["Virgin plastic"], "label": "Plastic packaging", "grp": "e5", "unit_family": "mass"}',))
            self.assertEqual(cur.fetchone()[0], {"ok": True, "matched": 1})
            cur.execute("select public.admin_metric_edit('k', 'plastic_packaging_used', %s::jsonb) ->> 'error', public.admin_metric_edit('k', 'plastic_packaging_used', %s::jsonb) ->> 'error', "
                        "public.admin_metric_edit('k', 'nope', '{}'::jsonb) ->> 'error'", ('{"aliases": ["Scope 1"]}', '{"kind": "sentence"}'))
            self.assertEqual(cur.fetchone(), ('"Scope 1" is already a name of "Scope 1 emissions"', "kind: number, yes_no, list, text, person or target", "no such metric"))
            cur.execute("select label, unit_family, aliases, status from public.fact_metrics where key = 'plastic_packaging_used'")
            self.assertEqual(cur.fetchone(), ("Plastic packaging", "mass", ["Virgin plastic"], "active"))
            # the old label is no longer a name: the facts keep their metric all the same
            cur.execute("select count(*) from public.ask_facts where metric = 'plastic_packaging_used'")
            self.assertEqual(cur.fetchone()[0], 3)
            # merge: names and facts go to the other metric, which then answers to the merged one's names
            cur.execute("select public.admin_metric_merge('k', 'plastic_packaging_used', 'waste_generated'), public.admin_metric_merge('k', 'waste_generated', 'waste_generated') ->> 'error'")
            merged, same = cur.fetchone()
            self.assertEqual((merged, same), ({"ok": True, "facts": 3}, "two different metrics are needed"))
            cur.execute("select public.fact_match('Virgin plastic'), public.fact_match('Plastic packaging'), (select status || ' ' || merged_into from public.fact_metrics where key = 'plastic_packaging_used')")
            self.assertEqual(cur.fetchone(), ("waste_generated", "waste_generated", "merged waste_generated"))
            # retire: the facts go back to unclassified and stay there
            cur.execute("select public.admin_metric_edit('k', 'scope_1', '{\"status\": \"retired\"}'::jsonb)")
            cur.execute("select metric from public.ask_facts where ask_id = 914")
            self.assertIsNone(cur.fetchone()[0])
            cur.execute("select public.admin_metric_edit('k', 'scope_1', '{\"status\": \"active\"}'::jsonb) -> 'matched'")
            self.assertGreaterEqual(cur.fetchone()[0], 1)
            # blocks and groups
            cur.execute("select public.admin_taxonomy_set('k', 'block', null, '{\"label\": \"Products & services\"}'::jsonb), "
                        "public.admin_taxonomy_set('k', 'group', null, '{\"label\": \"Animal welfare\", \"block\": \"products_services\"}'::jsonb)")
            blk, grp = cur.fetchone()
            self.assertEqual((blk["key"], grp["key"]), ("products_services", "animal_welfare"))
            cur.execute("select public.admin_taxonomy_set('k', 'group', 'animal_welfare', '{\"label\": \"Animals\", \"block\": \"sustainability\"}'::jsonb), "
                        "public.admin_taxonomy_set('k', 'group', null, '{\"label\": \"Orphan\"}'::jsonb) ->> 'error', public.admin_taxonomy_set('k', 'group', 'e1', '{\"block\": \"mars\"}'::jsonb) ->> 'error'")
            self.assertEqual(cur.fetchone()[1:], ("a group needs its block", "no such block"))
            cur.execute("select label, block from public.fact_groups where key = 'animal_welfare'")
            self.assertEqual(cur.fetchone(), ("Animals", "sustainability"))
            cur.execute("select count(*), count(*) filter (where before is not null) from public.fact_metric_log")
            changes, with_before = cur.fetchone()
            self.assertGreaterEqual(changes, 8); self.assertGreaterEqual(with_before, 5)
            c.rollback()

    def test_the_names_given_to_the_reader_put_the_questions_own_first(self):
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("insert into public.ask_facts (ask_id, wba_id, company, what, kind, value, verdict) values (921, 'PT_A', 'A Co', 'Cobalt sourced from recycled batteries', 'number', 5, null), "
                        "(922, 'PT_A', 'A Co', 'Cobalt mined by children', 'number', 1, 'wrong'), (923, 'PT_A', 'A Co', 'Pandas adopted', 'number', 1, null)")
            cur.execute("select public.ask_metric_names('How much water did the company withdraw, and its cobalt?', 5)")
            lines = cur.fetchone()[0].split("\n")
            self.assertEqual(len(lines), 6)                                             # five metrics and the one loose name that shares a word
            self.assertTrue(set(lines[:3]) == {"- Water withdrawal", "- Water consumption", "- Water discharge"} or "- Water target" in lines[:4], lines)
            self.assertEqual(lines[-1], "- Cobalt sourced from recycled batteries")      # not the one a person marked wrong, not the pandas
            cur.execute("select public.ask_metric_names('', 3), public.ask_metric_names(null, 3)")
            empty, none = cur.fetchone()
            self.assertEqual(len(empty.split("\n")), 3); self.assertEqual(len(none.split("\n")), 3)      # no word to go by: the most used metrics
            c.rollback()

    def test_a_companys_facts_come_with_their_metric_group_block_and_year(self):
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("insert into public.documents (library_id, wba_id, year, doc_type) values ('sheet-23', 'PT_SHEET', 2023, 'Sustainability Report') on conflict (library_id) do nothing")
            cur.execute("insert into public.ask_facts (ask_id, wba_id, company, what, kind, value, unit, answer, year, library_id, page, verdict) values "
                        "(931, 'PT_SHEET', 'Sheet Co', 'Scope 1 emissions', 'number', 100, 'tCO2e', null, 2022, 'sheet-23', 5, null), "
                        "(932, 'PT_SHEET', 'Sheet Co', 'Scope 1 emissions', 'number', 80, 'tCO2e', null, 2023, 'sheet-23', 5, 'right'), "
                        "(933, 'PT_SHEET', 'Sheet Co', 'Scope 1 emissions', 'number', 999, 'tCO2e', null, 2023, 'sheet-23', 6, 'wrong'), "
                        "(934, 'PT_SHEET', 'Sheet Co', 'Emissions verified', 'yes_no', null, null, 'yes', null, 'sheet-23', 9, null), "
                        "(935, 'PT_SHEET', 'Sheet Co', 'Colour of the logo', 'text', null, null, 'green', null, null, null, null)")
            cur.execute("select public.fact_rematch()")
            cur.execute("select public.admin_company_facts('k')")
            row = [x for x in cur.fetchone()[0]["companies"] if x["wba_id"] == "PT_SHEET"][0]
            self.assertEqual((row["company"], row["facts"], row["classified"]), ("Sheet Co", 4, 3))        # the wrong one is not counted
            cur.execute("select public.admin_company_facts('k', 'PT_SHEET')")
            sheet = cur.fetchone()[0]
            self.assertEqual(sheet["company"], "Sheet Co")
            got = [(f["metric"], f["block"], f["grp"], f["shown_year"], f["year_from"]) for f in sheet["facts"]]
            self.assertEqual(got, [("scope_1", "sustainability", "e1", 2023, "stated"), ("scope_1", "sustainability", "e1", 2022, "stated"),
                                   ("emissions_verified", "commitments", "assurance", 2023, "report"), (None, None, None, None, None)])   # the taxonomy's order, the unclassified last
            self.assertEqual(sheet["facts"][0]["doc_type"], "Sustainability Report"); self.assertEqual(sheet["facts"][3]["label"], "Colour of the logo")
            c.rollback()

    def test_the_owner_sees_every_company_with_its_documents_peoples_first(self):
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("insert into public.companies (wba_id, name, industry, country) values ('LN_zz000001', 'Zebra Added Co', 'Retail', 'Spain'), ('PT_ZZ9', 'Zebra Listed Co', 'Retail', 'Chile') on conflict (wba_id) do nothing")
            cur.execute("insert into public.documents (library_id, wba_id, year) values ('zz-1', 'PT_ZZ9', 2024), ('zz-2', 'PT_ZZ9', 2023) on conflict (library_id) do nothing")
            cur.execute("select public.admin_company_list('k', 'zebra retail')")
            got = cur.fetchone()[0]
            self.assertEqual([(r["id"], r["documents"], r["added"]) for r in got["rows"]], [("LN_zz000001", 0, True), ("PT_ZZ9", 2, False)])      # a person's first, then the most documents
            self.assertGreaterEqual(got["all"], 2); self.assertGreaterEqual(got["added"], 1)
            cur.execute("select jsonb_array_length(public.admin_company_list('k', 'zebra chile') -> 'rows'), jsonb_array_length(public.admin_company_list('k', 'zebra mars') -> 'rows'), "
                        "jsonb_array_length(public.admin_company_list('k', null, 1) -> 'rows')")
            self.assertEqual(cur.fetchone(), (1, 0, 1))
            c.rollback()

    def test_a_readers_reply_is_reduced_to_numbers_for_the_names_comparison(self):
        import json, psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            sources = [{"n": 1, "library_id": "x", "page": 3}, {"n": 2, "library_id": "x", "page": 4}]
            raw = ('Scope 1 was 120 tCO2e [1] and revenue 5 bn [2]. Verified [1].\nFACTS: ' + json.dumps([
                {"what": "Scope 1 emissions", "kind": "number", "value": "120", "source": 1},
                {"what": "Revenue", "kind": "number", "value": "5,000,000,000", "source": "[2]"},
                {"what": "Pandas adopted", "kind": "number", "value": "3", "source": 9},
                {"what": "Emissions verified", "kind": "yes_no", "answer": "yes", "source": 1}]))
            cur.execute("select public.ask_reply_numbers(%s, %s::jsonb, %s)", (raw, json.dumps(sources), "- Scope 1 emissions\n- Revenue\n- Net income"))
            got = cur.fetchone()[0]
            self.assertEqual((got["answered"], got["cites"], got["facts"], got["with_metric"], got["as_listed"]), (True, 3, 3, 3, 2))     # the fact with no page behind it is not counted
            self.assertEqual(sorted(got["values"]), ["120", "5000000000"]); self.assertEqual(len(got["names"]), 3)
            cur.execute("select public.ask_reply_numbers('no list here', '[]'::jsonb, '')")
            got = cur.fetchone()[0]
            self.assertEqual((got["answered"], got["facts"], got["values"]), (True, 0, []))
            cur.execute("select public.ask_names_trial(-1) ->> 'skipped'")
            self.assertEqual(cur.fetchone()[0], "no such answer")
            c.rollback()

    def test_a_fact_named_with_its_companys_name_still_finds_its_metric(self):
        """15 Nov 2026, the owner's screenshot: "Nike total revenues" and "NIKE, Inc. revenues" waited unclassified beside Revenue."""
        import json, psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cases = [("Nike total revenues", "Nike", "revenue"), ("NIKE, Inc. revenues", "Nike", "revenue"), ("Nike, Inc. total revenues", "NIKE, Inc.", "revenue"),
                     ("Nike chief executive officer", "Nike", "ceo"), ("Nike employees worldwide", "Nike", "employees"),
                     ("Scope 1 emissions", "Nike", "scope_1"),                       # the plain name, as before
                     ("Nike", "Nike", None), ("Nike, Inc.", "Nike", None),           # nothing but the company's name
                     ("Nike favourite colour", "Nike", None),
                     ("Water Corporation water withdrawal", "Water Corporation", None),      # a company named with the metric's own word: it waits unclassified, never a wrong metric
                     ("Revenue Solutions revenue", "Revenue Solutions", None),
                     ("Water withdrawal", "Water Corporation", "water_withdrawal"),          # the plain name is tried first, whatever the company is called
                     ("Nike total revenues", None, None), ("Nike total revenues", "", None), ("Adidas revenues", "Nike", None)]
            for name, company, want in cases:
                cur.execute("select public.fact_match(%s, %s)", (name, company))
                self.assertEqual(cur.fetchone()[0], want, (name, company))
            sources = [{"n": 1, "wba_id": "PT_NK", "company": "Nike", "library_id": "nk-1", "page": 4}]
            facts = [{"what": "Nike total revenues", "kind": "number", "value": "51220000000", "unit": "USD", "source": 1},
                     {"what": "Nike mascot", "kind": "text", "answer": "none", "source": 1}]
            cur.execute("select public.ask_keep_facts(951, %s::jsonb, %s::jsonb)", (json.dumps(facts), json.dumps(sources)))
            self.assertEqual([k["metric"] for k in cur.fetchone()[0]], ["revenue", None])
            cur.execute("insert into public.ask_facts (ask_id, wba_id, company, what, kind, value) values (952, 'PT_NK', 'Nike', 'NIKE, Inc. revenues', 'number', 5)")
            cur.execute("select public.fact_rematch()")
            self.assertGreaterEqual(cur.fetchone()[0], 1)
            cur.execute("select metric from public.ask_facts where ask_id = 952")
            self.assertEqual(cur.fetchone()[0], "revenue")
            c.rollback()

    def test_a_restated_figure_is_the_same_metric_with_a_mark(self):
        """15 Nov 2026, the owner: "Gross Scope 1 emissions · 2023" and "Gross Scope 1 emissions, restated · 2022" are the same thing."""
        import json, psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("select public.fact_restated('Gross Scope 1 emissions, restated'), public.fact_restated('Scope 1 (re-stated)'), public.fact_restated('Revised revenue'), "
                        "public.fact_restated('Scope 1 emissions'), public.fact_restated('Real estate assets'), public.fact_restated(null)")
            self.assertEqual(cur.fetchone(), (True, True, True, False, False, False))
            # the owner's second screenshot: these are about a restatement, they are not the restated figure
            cur.execute("select public.fact_restated('Restatement of 2023 Scope 1 emissions'), public.fact_restated('Reason for 2022 Scope 1 restatement'), "
                        "public.fact_restated('Scope 1 emissions 2022 restatement change'), public.fact_restated('Scope 1 emissions 2022 as originally reported'), public.fact_restated('Scope 1 emissions 2022 as restated')")
            self.assertEqual(cur.fetchone(), (False, False, False, False, True))
            cur.execute("select public.fact_match('Restatement of 2023 Scope 1 emissions', 'Amadeus', 'yes_no'), public.fact_match('Scope 1 emissions', 'Amadeus', 'yes_no'), "
                        "public.fact_match('Scope 1 emissions', 'Amadeus', 'number'), public.fact_match('Human rights policy', 'Amadeus', 'text'), public.fact_match('Scope 1 emissions 2022 as restated', 'Amadeus', 'number')")
            self.assertEqual(cur.fetchone(), (None, None, "scope_1", "human_rights_policy", "scope_1"))      # a metric that is a number takes figures only
            cur.execute("insert into public.documents (library_id, wba_id, year, doc_type) values ('am-23', 'PT_AM', 2023, 'Annual Report') on conflict (library_id) do nothing")
            sources = [{"n": 1, "wba_id": "PT_AM", "company": "Amadeus", "library_id": "am-23", "page": 126}]
            facts = [{"what": "Gross Scope 1 emissions", "kind": "number", "value": "1427", "unit": "t CO2", "year": 2023, "source": 1},
                     {"what": "Scope 1 emissions offset", "kind": "number", "value": "764", "unit": "t CO2", "year": 2023, "source": 1},
                     {"what": "Gross Scope 1 emissions, restated", "kind": "number", "value": "1493", "unit": "t CO2", "year": 2022, "source": 1},
                     {"what": "Scope 1 emissions", "kind": "number", "value": "1600", "unit": "t CO2", "year": 2022, "source": 1}]
            cur.execute("select public.ask_keep_facts(961, %s::jsonb, %s::jsonb)", (json.dumps(facts), json.dumps(sources)))
            kept = cur.fetchone()[0]
            self.assertEqual([(k["metric"], k["restated"]) for k in kept], [("scope_1", None), (None, None), ("scope_1", True), ("scope_1", None)])   # the offset is another thing
            cur.execute("select public.admin_company_facts('k', 'PT_AM')")
            rows = [(f["shown_year"], float(f["value"]), f["restated"]) for f in cur.fetchone()[0]["facts"] if f["metric"] == "scope_1"]
            self.assertEqual(rows, [(2023, 1427.0, False), (2022, 1493.0, True), (2022, 1600.0, False)])      # in its year the restated figure comes first
            # a later report gives 2022 again with another figure and does not say so: the latest report's figure comes first
            cur.execute("insert into public.documents (library_id, wba_id, year, doc_type) values ('am-24', 'PT_AM', 2024, 'Sustainability Report') on conflict (library_id) do nothing")
            cur.execute("insert into public.ask_facts (ask_id, wba_id, company, what, kind, value, unit, year, library_id, page, metric) values (962, 'PT_AM', 'Amadeus', 'Scope 1 emissions', 'number', 2148, 'tCO2eq', 2022, 'am-24', 114, 'scope_1')")
            cur.execute("select public.admin_company_facts('k', 'PT_AM')")
            rows = [(f["shown_year"], float(f["value"]), f["doc_year"]) for f in cur.fetchone()[0]["facts"] if f["metric"] == "scope_1" and f["shown_year"] == 2022]
            self.assertEqual(rows, [(2022, 2148.0, 2024), (2022, 1493.0, 2023), (2022, 1600.0, 2023)])
            # the two corrections the install makes: a yes under a metric that is a number, and a mark on a fact that is no figure
            cur.execute("insert into public.ask_facts (ask_id, wba_id, company, what, kind, answer, year, metric, restated) values (963, 'PT_AM', 'Amadeus', 'Restatement of 2023 Scope 1 emissions', 'yes_no', 'no', 2023, 'scope_1', true), "
                        "(964, 'PT_AM', 'Amadeus', 'Human rights policy', 'text', 'a policy', 2023, 'human_rights_policy', null)")
            import load_supabase as L
            cur.execute(L.guard_sql(open(os.path.join(pg_search.ROOT, "supabase", "search.sql"), encoding="utf-8").read()))
            cur.execute("select ask_id, metric, restated from public.ask_facts where ask_id in (961, 963, 964) and (ask_id <> 961 or restated) order by ask_id")
            self.assertEqual(cur.fetchall(), [(961, "scope_1", True), (963, None, None), (964, "human_rights_policy", None)])      # only the wrong ones are undone
            c.rollback()

    def test_the_owner_sees_what_each_model_said_in_its_latest_run(self):
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("insert into public.ask_facts (ask_id, wba_id, company, what, kind, value, metric) values (971, 'PT_T', 'Trial Co', 'Scope 1 emissions', 'number', 5, 'scope_1') returning id")
            a = cur.fetchone()[0]
            cur.execute("insert into public.ask_facts (ask_id, wba_id, company, what, kind, answer) values (972, 'PT_T', 'Trial Co', 'Reason for restatement', 'text', 'method') returning id")
            b = cur.fetchone()[0]
            cur.execute("insert into public.fact_trials (model, fact_id, metric, role, name, seconds, run) values "
                        "('m1', %s, 'revenue', 'value', '', 9, '100'), ('m1', %s, 'scope_1', 'value', '', 5, '200'), ('m1', %s, null, 'explanation', 'Restatement reason', 7, '200'), "
                        "('m2', %s, 'scope_1', 'restated', '', 11, '150'), ('m2', %s, null, null, null, 30, '150')", (a, a, b, a, b))
            cur.execute("select public.admin_fact_trials('k')")
            got = cur.fetchone()[0]
            self.assertEqual([(m["model"], m["run"], m["facts"], m["unanswered"]) for m in got["models"]], [("m1", "200", 2, 0), ("m2", "150", 2, 1)])     # the latest run of each model
            facts = {f["what"]: f for f in got["facts"] if f["company"] == "Trial Co"}
            self.assertEqual(facts["Scope 1 emissions"]["said"]["m1"], {"metric": "scope_1", "label": "Scope 1 emissions", "role": "value", "name": ""})   # not run 100's answer
            self.assertEqual(facts["Scope 1 emissions"]["said"]["m2"]["role"], "restated"); self.assertEqual(facts["Scope 1 emissions"]["metric_label"], "Scope 1 emissions")
            self.assertEqual(facts["Reason for restatement"]["said"]["m1"]["name"], "Restatement reason"); self.assertIsNone(facts["Reason for restatement"]["said"]["m2"]["role"])
            c.rollback()

    def test_the_owners_panel_links_facts_and_cost(self):
        import hashlib, psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("insert into public.ask_log (asker, question, answered, input_tokens, output_tokens) values ('p@x.org', 'q?', true, 1000000, 200000) returning id")
            ask_id = cur.fetchone()[0]
            cur.execute("insert into public.ask_facts (ask_id, wba_id, company, what, kind, value) values (%s, 'PT_1', 'Nike', 'Scope 1', 'number', 5) returning id", (ask_id,))
            fact = cur.fetchone()[0]
            cur.execute("select public.admin_ok('any')")
            self.assertTrue(cur.fetchone()[0])                                  # the test session is the owner
            cur.execute("select public.admin_link_new('k', 'Luis'), public.admin_link_new('k', 'no spaces please')")
            made, bad = cur.fetchone()
            cur.execute("select key_hash, active from public.search_keys where name = 'luis'")
            kept, active = cur.fetchone()
            self.assertEqual(kept, hashlib.sha256(made["key"].encode()).hexdigest())      # only the hash is kept
            self.assertTrue(active); self.assertIn("error", bad)
            cur.execute("select public.admin_link_new('k', 'luis') ->> 'error', public.admin_link_set('k', 'luis', false), public.admin_link_set('k', 'owner', false)")
            taken, off, owner = cur.fetchone()
            self.assertEqual(taken, "that name is taken"); self.assertTrue(off["ok"]); self.assertIn("error", owner)
            cur.execute("select active from public.search_keys where name = 'luis'")
            self.assertFalse(cur.fetchone()[0])
            cur.execute("select jsonb_array_length(public.admin_facts('k', 'nike', 'open', 10)), public.admin_fact_set('k', %s, 'wrong')", (fact,))
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute("select jsonb_array_length(public.admin_facts('k', null, 'all', 10, 'nike scope')), jsonb_array_length(public.admin_facts('k', null, 'all', 10, 'nike revenue')), "
                        "jsonb_array_length(public.admin_facts('k', null, 'all', 10, 'q?'))")
            self.assertEqual(cur.fetchone(), (1, 0, 1))                         # every word must be there; the question counts too
            cur.execute("select public.admin_facts('k', null, 'marked', 10)")
            marked = cur.fetchone()[0]
            self.assertEqual((marked[0]["verdict"], marked[0]["question"], marked[0]["asker"]), ("wrong", "q?", "p@x.org"))
            self.assertIn("doc_year", marked[0]); self.assertIn("doc_type", marked[0])       # the report the fact was read in
            cur.execute("select public.admin_fact_set('k', %s, 'none'), public.admin_overview('k')", (fact,))
            _, o = cur.fetchone()
            c.rollback()
        today = [d for d in o["days"] if d["tokens_in"] >= 1000000][0]
        self.assertAlmostEqual(float(today["usd"]), 0.2, places=3)             # 1M read at 0.10 + 0.2M written at 0.50
        self.assertIn("luis", [l["name"] for l in o["links"]])
        self.assertIn("p@x.org", [p["email"] for p in o["people"]])

    def test_a_page_is_read_inside_lantern(self):
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("select library_id, page from public.pages order by library_id, page limit 1")
            lid, page = cur.fetchone()
            cur.execute("select public.doc_page('k', %s, %s), public.doc_page('k', %s, 99999), public.doc_page('k', 'no such document', 1)", (lid, page, lid))
            got, beyond, none = cur.fetchone()
        self.assertTrue(got["body"]); self.assertEqual((got["library_id"], got["page"]), (lid, page)); self.assertIn("pages", got)
        self.assertIsNone(beyond["body"]); self.assertEqual(beyond["library_id"], lid)      # the document is there, the page is not
        self.assertIsNone(none)

    def test_an_answer_reads_the_newest_reports_among_the_best_pages(self):
        """ask_pages: the best five, then pages of the two newest years, then the rest. The search is replaced by a stub."""
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("""create or replace function public.find_blend(key text, q text, qvec real[] default null, company_filter text default null, year_from int default null, year_to int default null,
                             type_filter text default null, max_rows int default 50, client text default null, industry_filter text default null, country_filter text default null,
                             region_filter text default null, typed text default null)
                           returns table (log_id bigint, rank int, library_id text, company text, wba_id text, year int, doc_type text, label text, url text, page int, score real, excerpt text)
                           language sql as $f$
                             select null::bigint, g, 'L' || g, 'C', 'W', case when g in (9, 14) then 2026 when g = 20 then 2025 else 2021 end, 't', 'l', null::text, g, 0::real, 'e'
                             from generate_series(1, least(max_rows, 24)) g $f$""")
            cur.execute("select page, year from public.ask_pages('k', 'q', null, null, null, null, null, 'q?')")
            got = cur.fetchall()
            c.rollback()
        self.assertEqual([p for p, _ in got], [1, 2, 3, 4, 5, 9, 14, 20])       # the best five, then 2026 and 2025 before older pages ranked above them

    def test_an_answer_with_no_year_reads_the_newest_main_report_too(self):
        """Option ask_latest: the search's first pages are all of an old report; the company's newest main report is asked for on its own."""
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("""create or replace function public.find_blend(key text, q text, qvec real[] default null, company_filter text default null, year_from int default null, year_to int default null,
                             type_filter text default null, max_rows int default 50, client text default null, industry_filter text default null, country_filter text default null,
                             region_filter text default null, typed text default null)
                           returns table (log_id bigint, rank int, library_id text, company text, wba_id text, year int, doc_type text, label text, url text, page int, score real, excerpt text)
                           language sql as $f$
                             select null::bigint, g, case when year_from is null then 'OLD' else 'NEW' end, 'C', 'W', coalesce(year_from + 1, 2022), 't', 'l', null::text,
                                    case when year_from is null then g else 100 + g end, 0::real, 'e'
                             from generate_series(1, least(max_rows, 24)) g $f$""")
            cur.execute("create or replace function public.find_company_ids(company_filter text) returns text[] language sql as $f$ select array['W_LATEST'] $f$")
            cur.execute("insert into public.documents (library_id, wba_id, company, doc_type, year, pages) values ('w-new', 'W_LATEST', 'C', 'Sustainability Report', 2025, 120), ('w-short', 'W_LATEST', 'C', 'Annual Report', 2027, 2), ('w-other', 'W_LATEST', 'C', 'Statutory Documents', 2027, 50)")
            ask = "select page, year from public.ask_pages('k', 'q', null, %s, %s, null, null, 'q?')"
            cur.execute("select set_config('lantern.ask_latest', 'off', true)")          # the option is on since 15 Nov 2026: a session can still switch it off (the comparison does)
            cur.execute(ask, ("C", None)); off = cur.fetchall()
            cur.execute("select set_config('lantern.ask_latest', '', true)")
            cur.execute(ask, ("C", None)); default = cur.fetchall()
            cur.execute("select set_config('lantern.ask_latest', 'on', true)")
            cur.execute(ask, ("C", None)); on = cur.fetchall()
            cur.execute(ask, (None, None)); no_company = cur.fetchall()
            cur.execute(ask, ("C", 2022)); a_year = cur.fetchall()
            c.rollback()
        self.assertEqual([p for p, _ in off], [1, 2, 3, 4, 5, 6, 7, 8])                             # as before: the option is off
        self.assertEqual([p for p, _ in default], [1, 2, 3, 4, 5, 101, 102, 103])                   # the default is the option
        self.assertEqual(on, [(1, 2022), (2, 2022), (3, 2022), (4, 2022), (5, 2022), (101, 2025), (102, 2025), (103, 2025)])      # 2025: a two-page annual report and a statutory document do not set the year
        self.assertEqual([p for p, _ in no_company], [1, 2, 3, 4, 5, 6, 7, 8])                      # no company named: nothing to be the newest of
        self.assertEqual(a_year, [(100 + g, 2023) for g in range(1, 9)])                            # a year asked for: one search, its eight pages (the stub marks a search with a year)

    def test_an_answer_about_a_year_reads_the_later_reports_too(self):
        """Option ask_later (15 Nov 2026): a figure of 2023 is printed again, sometimes restated, by the reports of 2024 and 2025."""
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("""create or replace function public.find_blend(key text, q text, qvec real[] default null, company_filter text default null, year_from int default null, year_to int default null,
                             type_filter text default null, max_rows int default 50, client text default null, industry_filter text default null, country_filter text default null,
                             region_filter text default null, typed text default null)
                           returns table (log_id bigint, rank int, library_id text, company text, wba_id text, year int, doc_type text, label text, url text, page int, score real, excerpt text)
                           language sql as $f$
                             select null::bigint, g, case when year_to is null then 'LATER' else 'ASKED' end, 'C', 'W', case when year_to is null then year_from else year_to end, 't', 'l', null::text,
                                    case when year_to is null then 200 + g else g end, 0::real, client
                             from generate_series(1, least(max_rows, 24)) g $f$""")
            cur.execute("create or replace function public.find_company_ids(company_filter text) returns text[] language sql as $f$ select array['W_LATER'] $f$")
            cur.execute("insert into public.documents (library_id, wba_id, company, doc_type, year, pages) values ('l-23', 'W_LATER', 'C', 'Annual Report', 2023, 200), ('l-25', 'W_LATER', 'C', 'Annual Report', 2025, 200), "
                        "('l-short', 'W_LATER', 'C', 'Annual Report', 2027, 2), ('l-other', 'W_LATER', 'C', 'Statutory Documents', 2027, 50)")
            ask = "select page, year, excerpt from public.ask_pages('k', 'q', null, %s, %s, %s, null, 'q?')"
            cur.execute(ask, ("C", 2023, 2023)); default = cur.fetchall()
            cur.execute("select set_config('lantern.ask_later', 'on', true)")
            cur.execute(ask, ("C", 2023, 2023)); on = cur.fetchall()
            cur.execute(ask, ("C", 2021, 2023)); span = cur.fetchall()
            cur.execute(ask, ("C", 2025, 2025)); newest = cur.fetchall()
            cur.execute(ask, (None, 2023, 2023)); no_company = cur.fetchall()
            cur.execute("select set_config('lantern.ask_later', 'off', true)")
            cur.execute(ask, ("C", 2023, 2023)); off = cur.fetchall()
            c.rollback()
        eight = [(g, 2023, "ask") for g in range(1, 9)]
        self.assertEqual(default, eight); self.assertEqual(off, eight)                               # the option is off until it is compared
        self.assertEqual(on, [(g, 2023, "ask") for g in range(1, 6)] + [(200 + g, 2024, "ask later") for g in range(1, 4)])      # the best five of the year asked, then three pages of later reports
        self.assertEqual([p for p, _, _ in span], [1, 2, 3, 4, 5, 201, 202, 203])                   # a span of years: later than its last year
        self.assertEqual(newest, [(g, 2025, "ask") for g in range(1, 9)])                            # no main report after 2025 (two pages, or statutory, do not count)
        self.assertEqual(no_company, eight)                                                         # no company named: whose later reports?

    def test_a_fiscal_period_gets_wbas_year_and_is_one_period_however_written(self):
        import json, psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("select public.fiscal_label_year('FY24'), public.fiscal_label_year('FISCAL 2023'), public.fiscal_label_year('FY20–25'), public.fiscal_label_year('2024'), "
                        "public.period_key('fy 23') = public.period_key('Fiscal Year 2023'), public.wba_year(2024, 5), public.wba_year(2024, 6), public.wba_year(2024, null)")
            self.assertEqual(cur.fetchone(), (2024, 2023, None, None, True, 2023, 2024, None))
            cur.execute("insert into public.company_fiscal (wba_id, fy_end_month, source) values ('PT_MAY', 5, 'pages'), ('PT_DEC', 12, 'period_end')")
            facts = [{"what": "Revenue", "kind": "number", "value": 51217, "unit": "USD m", "period": "FISCAL 2023", "source": 1},
                     {"what": "Revenue", "kind": "number", "value": 35120, "unit": "EUR m", "period": "FY25", "source": 2}]
            sources = [{"n": 1, "company": "May Co", "wba_id": "PT_MAY", "library_id": "L1", "page": 35}, {"n": 2, "company": "Dec Co", "wba_id": "PT_DEC", "library_id": "L2", "page": 12}]
            cur.execute("select public.ask_keep_facts(77, %s::jsonb, %s::jsonb), public.ask_fiscal_lines(%s::jsonb)", (json.dumps(facts), json.dumps(sources), json.dumps(sources)))
            kept, lines = cur.fetchone()
            cur.execute("update public.ask_facts set verdict = 'right', verdict_by = 'a@x.org', verdict_at = now() where ask_id = 77 and wba_id = 'PT_MAY'")
            cur.execute("insert into public.ask_facts (ask_id, wba_id, company, what, kind, value, period) values (78, 'PT_MAY', 'May Co', 'Total revenues', 'number', 51217, 'FY23')")
            cur.execute("select public.company_facts('k', 'PT_MAY')")
            later = cur.fetchone()[0]
            c.rollback()
        self.assertEqual([k["wba_year"] for k in kept], [2022, 2025])            # a year ending in May is the reporting year before; December is its own
        self.assertIn("May Co: the fiscal year ends in May", lines); self.assertNotIn("Dec Co", lines)
        self.assertEqual(later["open"], [])                                       # "FY23" repeats the confirmed "FISCAL 2023": not offered again

    def test_the_details_of_a_value_and_its_other_readings(self):
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("select library_id, wba_id from public.documents where wba_id is not null order by library_id limit 2")
            (l1, wba), (l2, _) = cur.fetchall()
            for vid, lid, value, head in (("v-one", l1, 66830, "FY20"), ("v-two", l2, 66831, "FY2020")):
                cur.execute("insert into public.value_versions (version_id, wba_id, attribute, year, value, unit, printed, scale, column_label, library_id, page, method) "
                            "values (%s, %s, 'GMC_00001.1', 2019, %s, 'tCO2e', '66,830', 1, %s, %s, 23, 'model')", (vid, wba, value, head, lid))
            cur.execute("insert into public.company_fiscal (wba_id, fy_end_month, source) values (%s, 5, 'pages') on conflict (wba_id) do update set fy_end_month = 5", (wba,))
            cur.execute("select public.value_detail('k', 'v-one'), public.value_detail('k', 'no such value')")
            g, none = cur.fetchone()
            c.rollback()
        self.assertEqual((g["column_label"], g["printed"], g["year"], g["fy_end_month"], g["page"]), ("FY20", "66,830", 2019, 5, 23))
        self.assertEqual(sorted((r["value"], r["this"]) for r in g["readings"]), [(66830, True), (66831, False)])
        self.assertIsNone(none)

    def test_a_gap_card_shows_a_company_with_a_main_report_missing(self):
        import datetime, psycopg
        last = datetime.date.today().year - 1
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("create table if not exists public.companies (wba_id text primary key, name text, industry text, country text, region text, website text, lantern_id text)")
            cur.execute("delete from public.companies")
            cur.execute("insert into public.companies (wba_id, name, industry, country, website) values ('GAP_FULL', 'Full Co', 'I', 'C', 'full.example'), ('GAP_HOLE', 'Hole Co', 'I', 'C', 'hole.example')")
            for wba, year, kind in [("GAP_FULL", y, k) for y in (last, last - 1, last - 2) for k in ("Integrated Report",)] + [("GAP_HOLE", last, "Annual Report"), ("GAP_HOLE", last - 1, "Policy Documents")]:
                cur.execute("insert into public.documents (library_id, wba_id, company, doc_type, year) values (%s, %s, %s, %s, %s)", (f"{wba}-{year}-{kind}", wba, wba, kind, year))
            cur.execute("select public.gap_card('k', null), public.gap_card('k', array['GAP_HOLE'])")
            got, skipped = cur.fetchone()
            c.rollback()
        self.assertEqual((got["wba_id"], got["gaps"], got["website"]), ("GAP_HOLE", 3, "hole.example"))       # an integrated report is both: Full Co has no gap
        years = {y["year"]: y for y in got["years"]}
        self.assertEqual((years[last]["annual"], years[last]["sustainability"]), (["Annual Report"], []))
        self.assertEqual((years[last - 1]["annual"], years[last - 1]["documents"]), ([], 1))                    # a policy document is not a main report
        self.assertIsNone(skipped)                                                                               # already shown to this person

    def test_a_gap_a_person_explained_and_a_sustainability_chapter_are_not_gaps(self):
        import datetime, psycopg
        last = datetime.date.today().year - 1
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("create table if not exists public.companies (wba_id text primary key, name text, industry text, country text, region text, website text, lantern_id text)")
            cur.execute("delete from public.companies"); cur.execute("delete from public.gap_notes")
            cur.execute("insert into public.companies (wba_id, name) values ('GAP_CSRD', 'Chapter Co'), ('GAP_PRIV', 'Private Co')")
            for y in (last, last - 1, last - 2):                 # Chapter Co: an annual report each year, no sustainability report
                cur.execute("insert into public.documents (library_id, wba_id, company, doc_type, year) values (%s, 'GAP_CSRD', 'Chapter Co', 'Annual Report', %s)", (f"csrd-{y}", y))
                for page in (1, 2, 3):
                    body = "sustainability statements prepared under ESRS" if y >= last - 1 else "the year in brief"
                    cur.execute("insert into public.pages (library_id, page, body, words, year, doc_type, wba_id) values (%s, %s, %s, to_tsvector('simple', %s), %s, 'Annual Report', 'GAP_CSRD')",
                                (f"csrd-{y}", page, body, body, y))
                cur.execute("insert into public.documents (library_id, wba_id, company, doc_type, year) values (%s, 'GAP_PRIV', 'Private Co', 'Sustainability Report', %s)", (f"priv-{y}", y))
            cur.execute("select public.gap_card('k', array['GAP_PRIV'])")
            chapter = cur.fetchone()[0]
            # a person says so: Private Co publishes no accounts (every year); Chapter Co's oldest year is not published
            cur.execute("select public.gap_note_set('k', 'GAP_PRIV', 'annual', 0, 'not_published')")
            cur.execute("select public.gap_card('k', array['GAP_CSRD'])")
            private = cur.fetchone()[0]
            cur.execute("select public.gap_note_set('k', 'GAP_PRIV', 'annual', %s, 'gap')", (last,))            # one year taken back
            cur.execute("select public.gap_card('k', array['GAP_CSRD'])")
            back = cur.fetchone()[0]
            cur.execute("select public.gap_note_set('k', 'GAP_CSRD', 'sustainability', %s, 'not_published')", (last - 2,))
            cur.execute("select public.gap_card('k', array['GAP_PRIV'])")
            explained = cur.fetchone()[0]
            bad = []
            for args in (("GAP_PRIV", "annual", 0, "inside_annual"), ("GAP_PRIV", "annual", 0, "not_yet"), ("GAP_PRIV", "annual", 0, "because"), ("NO_SUCH", "annual", 0, "parent")):
                try:
                    with c.transaction():
                        cur.execute("select public.gap_note_set('k', %s, %s, %s, %s)", args)
                    bad.append(args)
                except psycopg.Error:
                    pass
            c.rollback()
        years = {y["year"]: y for y in chapter["years"]}
        self.assertEqual(chapter["gaps"], 1)                                             # the two years whose annual report carries the statements are not gaps
        self.assertTrue(years[last]["sustainability_inside"] and years[last - 1]["sustainability_inside"])
        self.assertNotIn("sustainability_inside", years[last - 2])
        self.assertIsNone(private)                                                       # every gap explained: no card
        self.assertEqual((back["wba_id"], back["gaps"]), ("GAP_PRIV", 1))
        self.assertEqual({y["year"]: y["annual_note"] for y in back["years"]}, {last: "gap", last - 1: "not_published", last - 2: "not_published"})
        self.assertIsNone(explained)
        self.assertEqual(bad, [])                                                        # only the answers of the list, for a company that exists

    def test_did_you_mean(self):
        import psycopg, load_supabase as L
        with psycopg.connect(URL) as c:
            L.refresh_derived(c)
        cur = self.conn.cursor()
        cur.execute("select public.did_you_mean('', 'unilver scope 3')"); self.assertEqual(cur.fetchone()[0], "unilever scope 3")
        cur.execute("select public.did_you_mean('', 'living wage')"); self.assertIsNone(cur.fetchone()[0])

    def test_the_review_card_lists_every_reading_and_carries_the_page_when_there_is_no_link(self):
        """12 Nov 2026: the card said "1 give another" and not which figure or where; and a document with no address
        could not be opened at all."""
        cur = self.conn.cursor()
        cur.execute("delete from public.value_versions")
        cur.execute("insert into public.documents (library_id, wba_id, company, doc_type, year, label, url, pages) values "
                    "('PT_09999-new', 'PT_09999', 'Card Co', 'ESG Report', 2026, '2026 | Card Co | ESG Report', null, 3), "
                    "('PT_09999-old', 'PT_09999', 'Card Co', 'ESG Report', 2025, '2025 | Card Co | ESG Report', 'https://card.example/esg-2025.pdf', 3) "
                    "on conflict (library_id) do nothing")
        cur.execute("insert into public.pages (library_id, page, body) values ('PT_09999-new', 2, %s) on conflict do nothing",
                    ("x " * 1200 + "Scope 2 emissions 150,641 tonnes " + "y " * 2500,))
        cur.execute("insert into public.value_versions (version_id, wba_id, attribute, year, value, unit, printed, library_id, page, method) values "
                    "('v-new', 'PT_09999', 'GMC_T', 2024, 150641, 'tCO2e', '150,641', 'PT_09999-new', 2, 'model'), "
                    "('v-old', 'PT_09999', 'GMC_T', 2024, 148000, 'tCO2e', '148,000', 'PT_09999-old', 3, 'model')")
        seen = {}
        for _ in range(40):                                         # the draw is random between the two
            cur.execute("select public.lucky_value()")
            v = cur.fetchone()[0]["value"]
            seen[v["library_id"]] = v
            if len(seen) == 2:
                break
        new, old = seen["PT_09999-new"], seen["PT_09999-old"]
        self.assertEqual([(r["value"], r["doc_year"], r["this"]) for r in new["readings"]], [(150641, 2026, True), (148000, 2025, False)])
        self.assertEqual([r["this"] for r in old["readings"]], [False, True])
        self.assertEqual(old["readings"][1]["url"], "https://card.example/esg-2025.pdf")
        self.assertIsNone(new["url"]); self.assertGreater(len(new["excerpt"]), 4000); self.assertIn("150,641", new["excerpt"])
        self.assertEqual((new["differ"], new["others"]), (1, 0))
        cur.execute("delete from public.value_versions; delete from public.pages where library_id like 'PT_09999-%'; delete from public.documents where library_id like 'PT_09999-%'")

    def test_the_unit_shown_drops_the_scale_that_went_into_the_value(self):
        """12 Nov 2026: 0.39 Mt-CO2e was shown as 390,000 Mt-CO2e. The same cases as model_reader.unit_after_scale."""
        from test_model_reader import UNIT_CASES
        cur = self.conn.cursor()
        for unit, scale, want in UNIT_CASES:
            cur.execute("select public.unit_shown(%s::text, %s::numeric, %s::text)", (unit, 0.39 * scale, "0.39"))
            self.assertEqual(cur.fetchone()[0], want, (unit, scale))
        cur.execute("select public.unit_shown('Mt-CO2e', 390000, null::text), public.unit_shown('Mt-CO2e', 390000, '12,345'), public.unit_shown('RMB''000', 12575901000, '12,575,901')")
        self.assertEqual(cur.fetchone(), ("Mt-CO2e", "Mt-CO2e", "RMB"))             # no printed number or another ratio: untouched

    def test_the_schema_installs_while_other_jobs_read_and_write(self):
        """The semantic index keeps a transaction open on pages, chunks and page_pairs for hours; the load applies
        schema.sql at its start and waits 30 seconds at most for a lock. Nothing in the guarded schema may need a
        lock those readers and writers refuse (12 Nov 2026: "drop trigger" on pages did, and the load ended there)."""
        import psycopg, load_supabase as LS
        other = psycopg.connect(URL)
        try:
            oc = other.cursor()
            oc.execute("select count(*) from public.pages"); oc.execute("select count(*) from public.documents")
            oc.execute("delete from public.chunks where false"); oc.execute("delete from public.page_pairs where false")
            with psycopg.connect(URL) as mine:                  # `other` is now idle in a transaction that holds its locks
                c = mine.cursor()
                c.execute("set lock_timeout = '2s'")
                c.execute(LS.guard_sql(pg_search.schema_text(c)))
                mine.commit()
        finally:
            other.rollback(); other.close()

if __name__ == "__main__":
    unittest.main()
