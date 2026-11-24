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

    def test_group_first_finds_the_same_pages_for_a_group(self):
        """Option group_first (24 Nov 2026): a search within a picked group of six companies or more reads the group's own
        pages; the list is the same as through the word index, and nothing changes while the option is off."""
        cur = self.conn.cursor()
        cur.execute("select is_on from public.search_options where name = 'group_first'"); self.assertEqual(cur.fetchone(), (False,))
        cur.execute("select string_agg(wba_id, ';' order by wba_id) from (select distinct wba_id from public.documents where wba_id <> '' order by wba_id limit 8) x"); group = cur.fetchone()[0]
        self.assertGreaterEqual(group.count(";"), 5)
        cur.execute("select set_config('lantern.group_first', 'off', false)")
        usual = self.find("zebra target", group)
        cur.execute("select set_config('lantern.group_first', 'on', false)")
        try:
            first = self.find("zebra target", group)
        finally:
            cur.execute("select set_config('lantern.group_first', '', false)")
        self.assertTrue(usual)
        self.assertEqual(sorted((r[0], r[1]) for r in first), sorted((r[0], r[1]) for r in usual))

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

    def test_of_the_copies_of_a_document_the_list_shows_the_one_that_was_read(self):
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("insert into public.documents (library_id, wba_id, company, doc_type, year, label, pages, text_status) values "
                        "('cp-a', 'PT_COPY', 'Copy Co', 'Annual Report', 2024, '2024 | Copy Co | ANNUAL REPORT 2024', 0, 'no_file'), "
                        "('cp-m', 'PT_COPY', 'Copy Co', 'Annual Report', 2024, '2024 | Copy Co | Annual Report 2024', 158, 'ok'), "
                        "('cp-z', 'PT_COPY', 'Copy Co', 'Annual Report', 2024, '2024 | Copy Co | ANNUAL REPORT 2024', 0, 'no_file'), "
                        "('cp-o', 'PT_COPY', 'Copy Co', 'Annual Report', 2023, '2023 | Copy Co | ANNUAL REPORT 2023', 0, 'no_file')")
            cur.execute("create or replace function public.find_company_ids(company_filter text) returns text[] language sql as $f$ select array['PT_COPY'] $f$")
            cur.execute("select library_id from public.find_pages('k', '', 'Copy Co')"); got = [r[0] for r in cur.fetchall()]
            cur.execute("select public.doc_facts('k', array['cp-m', 'cp-o'])"); facts = cur.fetchone()[0]
            c.rollback()
        self.assertEqual(got, ["cp-m", "cp-o"])                                    # one row a title and year: the copy with pages; a document nobody read still shows
        self.assertEqual((facts["cp-m"].get("status"), facts["cp-o"]["status"]), (None, "no_file"))

    def test_the_reader_is_given_3000_characters_of_a_page_or_8000_with_the_option(self):
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("select public.reader_page_chars(), (select is_on from public.search_options where name = 'reader_whole_page')"); default = cur.fetchone()
            cur.execute("select set_config('lantern.reader_whole_page', 'off', true)"); cur.execute("select public.reader_page_chars()"); off = cur.fetchone()[0]
            cur.execute("select set_config('lantern.reader_whole_page', 'on', true)"); cur.execute("select public.reader_page_chars()"); on = cur.fetchone()[0]
            cur.execute("select has_function_privilege('anon', 'public.reader_page_chars()', 'execute')"); public = cur.fetchone()[0]
            c.rollback()
        self.assertEqual((default, off, on, public), ((8000, True), 3000, 8000, False))   # on since 19 Nov 2026; a comparison can still read 3,000

    def test_what_the_library_holds_for_a_company_is_said_from_the_index(self):
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("insert into public.documents (library_id, wba_id, company, doc_type, year, label, pages, text_status) values "
                        "('cd-1', 'PT_CARD', 'Card Co', 'Annual Report', 2024, 'AR 2024', 158, 'ok'), ('cd-2', 'PT_CARD', 'Card Co', 'Annual Report', 2024, 'ar 2024', 0, 'no_file'), "
                        "('cd-3', 'PT_CARD', 'Card Co', 'Annual Report', 2023, 'AR 2023', 0, 'no_file'), ('cd-4', 'PT_CARD', 'Card Co', 'Annual Report', 2025, 'AR 2025', 0, 'no_file'), "
                        "('cd-5', 'PT_CARD', 'Card Co', 'Code of Conduct', 2023, 'Code', 27, 'ok'), ('cd-6', 'PT_CARD', 'Card Co', 'Code of Conduct', null, 'Code for others', 12, 'ok'), "
                        "('cd-7', 'PT_CARD', 'Card Co', 'Policy Documents', 2020, 'Privacy', 3, 'no_text')")
            cur.execute("select public.company_card('k', 'PT_CARD')"); self.assertEqual(cur.fetchone()[0]["published"], [])        # no copy of the library here: no dates, no error
            # v129: when the main reports came out, from the library's copy (the last five reporting years; a policy is not a main report)
            cur.execute("create table if not exists public.library (library_id text primary key, record_id text, wba_id text, discard boolean not null default false, fields jsonb not null)")
            cur.execute("update public.documents set year = extract(year from now())::int - 1 where library_id = 'cd-4'")
            cur.execute("update public.documents set year = extract(year from now())::int - 2 where library_id in ('cd-1', 'cd-2')")
            cur.execute("insert into public.library (library_id, wba_id, fields) values ('cd-1', 'PT_CARD', '{\"pub_date\": \"2025-03-04\", \"pub_date_precision\": \"day\", \"pub_date_source\": \"filing\"}'), "
                        "('cd-4', 'PT_CARD', '{\"pub_date\": \"2026-02-01\", \"pub_date_precision\": \"month\"}'), ('cd-5', 'PT_CARD', '{\"pub_date\": \"2023-01-01\"}'), ('cd-3', 'PT_CARD', '{\"pub_date\": \"\"}')")
            cur.execute("select public.company_card('k', 'PT_CARD')"); pubs = cur.fetchone()[0]["published"]
            self.assertEqual([(p["date"], p["precision"], p["type"]) for p in pubs], [("2026-02-01", "month", "Annual Report"), ("2025-03-04", "day", "Annual Report")])
            cur.execute("update public.documents set year = 2025 where library_id = 'cd-4'"); cur.execute("update public.documents set year = 2024 where library_id in ('cd-1', 'cd-2')")
            cur.execute("select public.company_card('k', 'PT_CARD'), public.company_card('k', 'PT_NOBODY')"); card, nobody = cur.fetchone()
            cur.execute("select has_function_privilege('anon', 'public.company_card(text, text)', 'execute')"); public = cur.fetchone()[0]
            c.rollback()
        self.assertEqual((card["company"], card["documents"], card["read"], card["unread"], card["first"], card["last"]), ("Card Co", 6, 3, 3, 2020, 2025))   # a document held twice counts once
        self.assertEqual(card["types"][0], {"type": "Annual Report", "n": 3, "years_read": [2024], "years_unread": [2023, 2025], "undated": 0})          # the main reports first
        self.assertEqual({t["type"]: (t["years_read"], t["years_unread"], t["undated"]) for t in card["types"][1:]},
                         {"Code of Conduct": ([2023], [], 1), "Policy Documents": ([], [2020], 0)})                                                         # a file with no text is not read
        self.assertIsNone(nobody); self.assertTrue(public)

    def test_a_broad_topic_is_told_and_its_overview_reads_a_page_of_each_part(self):
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            topic = lambda t: (cur.execute("select public.topic_of(%s)", (t,)), cur.fetchone()[0])[1]
            env = topic("environmental impact"); water = topic("overview of the Water data"); spanish = topic("Impacto ambiental")
            none = [topic(t) for t in ("biodiversity approach", "steel production", "", "trees plants", "business model", "What is the business model of")]      # 22 Nov 2026: a business model is searched, it is not the overview of the business
            self.assertEqual(topic("main business")["key"], "business")
            cur.execute("select public.find_topics('k')"); names = cur.fetchone()[0]
            # each search gives the pages of its words: three a metric, the metric's name in the excerpt
            cur.execute("""create or replace function public.find_blend(key text, q text, qvec real[] default null, company_filter text default null, year_from int default null, year_to int default null,
                             type_filter text default null, max_rows int default 50, client text default null, industry_filter text default null, country_filter text default null,
                             region_filter text default null, typed text default null)
                           returns table (log_id bigint, rank int, library_id text, company text, wba_id text, year int, doc_type text, label text, url text, page int, score real, excerpt text)
                           language sql as $f$
                             select null::bigint, g, 'DOC', 'C', company_filter, 2025, 'Sustainability Report', 'l', null::text,
                                    mod(abs(hashtext(q)), 90) * 10 + g, 0::real, q || ' / ' || client
                             from generate_series(1, least(max_rows, 3)) g $f$""")
            cur.execute("select page, excerpt from public.overview_pages('k', 'PT_X', null, null, %s::jsonb)", (psycopg.types.json.Jsonb(env),)); pages = cur.fetchall()
            cur.execute("select has_function_privilege('authenticated', 'public.overview_pages(text, text, int, int, jsonb)', 'execute'), has_function_privilege('anon', 'public.topic_of(text)', 'execute')")
            closed = cur.fetchone()
            c.rollback()
        self.assertEqual((env["key"], [g["key"] for g in env["groups"]]), ("environment", ["e1", "e3", "e5", "e2", "e4"]))
        self.assertTrue(all(1 <= len(g["metrics"]) <= 3 for g in env["groups"]))               # five parts: one metric each, so that eight searches cover them
        self.assertEqual((water["key"], [g["key"] for g in water["groups"]], spanish["key"]), ("e3", ["e3"], "environment"))      # a group is a topic too; no accents, any language held
        self.assertEqual(none, [None] * 6)                                                      # the text must be the topic and nothing else
        self.assertIn("environmental impact", names); self.assertIn("water", names); self.assertIn("derechos humanos", names)
        self.assertEqual(len(pages), 8); self.assertEqual(len({p for p, _ in pages}), 8)        # eight pages, none twice
        first_five = [e.split(" / ")[0] for _, e in pages[:5]]
        self.assertEqual(len(set(first_five)), 5)                                               # the first page of each of the five parts before the second of any
        self.assertTrue(all(e.endswith("/ overview") for _, e in pages)); self.assertEqual(closed, (False, False))

    def test_a_persons_own_link_may_ask_without_a_sign_in_when_the_owner_says_so(self):
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("insert into public.search_keys (name, key_hash) values ('pers', encode(extensions.digest('secret-1', 'sha256'), 'hex')), ('team', encode(extensions.digest('secret-t', 'sha256'), 'hex')) "
                        "on conflict (name) do update set key_hash = excluded.key_hash, active = true")
            may = lambda k: (cur.execute("select public.link_may_ask(%s), public.link_asker(%s)", (k, k)), cur.fetchone())[1]
            before = may("secret-1")
            cur.execute("select public.admin_link_ask('k', 'pers', true), public.admin_link_ask('k', 'team', true), public.admin_link_ask('k', 'nobody', true)"); set_on = cur.fetchone()
            cur.execute("update public.search_keys set may_ask = true where name = 'team'")          # even set by hand, the shared link does not ask
            on, team, wrong = may("secret-1"), may("secret-t"), may("another")
            cur.execute("create or replace function auth.jwt() returns jsonb language sql as $f$ select '{}'::jsonb $f$")   # nobody signed in; undone by the rollback
            cur.execute("savepoint s"); refused = None
            try:
                cur.execute("select public.ask('another', 'What were the scope 1 emissions of Acme in 2024?')")
            except psycopg.errors.InsufficientPrivilege as e:
                refused = str(e).splitlines()[0]
            cur.execute("rollback to savepoint s")
            cur.execute("select public.ask('secret-1', 'What were the scope 1 emissions of Acme in 2024?')"); got = cur.fetchone()[0]
            cur.execute("update public.search_keys set active = false where name = 'pers'"); off = may("secret-1")
            cur.execute("select has_function_privilege('anon', 'public.ask(text, text, text, text, int, int, text, text, real[], text, text, text, boolean)', 'execute'), "
                        "has_function_privilege('anon', 'public.link_asker(text)', 'execute'), has_function_privilege('anon', 'public.admin_link_ask(text, text, boolean)', 'execute')")
            grants = cur.fetchone()
            c.rollback()
        self.assertEqual(before, (False, None))                                              # off until the owner turns it on
        self.assertEqual((set_on[0], set_on[2]), ({"ok": True}, {"ok": False})); self.assertIn("error", set_on[1])
        self.assertEqual((on, team, wrong, off), ((True, "link:pers"), (False, None), (False, None), (False, None)))
        self.assertIn("sign in", refused)                                                    # no sign-in and no such link: refused
        self.assertNotIn("error", got); self.assertIn("per_day", got)                        # the link's own allowance: it got past the door (no model in the tests)
        self.assertEqual(grants, (True, False, False))

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
        for bad in (("PT_nope", "a.pdf", 5000, sha, None), ("PT_00000", "a.pdf", 10, sha, None), ("PT_00000", "a.pdf", 315000000, sha, None),
                    ("PT_00000", "a.pdf", 5000, "zz", None), ("PT_00000", "a.pdf", 5000, "cd" * 32, "not a link")):
            with self.assertRaises(Exception):
                cur.execute("select public.upload_begin(%s, %s, %s, %s, %s)", bad)
        cur.execute("select jsonb_array_length(public.upload_companies()) > 0"); self.assertTrue(cur.fetchone()[0])
        # "Send this page" (extension 0.7.0): the page's text becomes a document of type Web page, searchable at once
        text = "\n\n".join(f"Paragraph {i} about our living wage commitment for suppliers. " * 12 for i in range(12)) + "\n\n" + "x" * 7000
        cur.execute("select public.page_capture('PT_00000', 'https://example.com/policies/wages#top', '  Wages  policy ', %s)", (text,)); cap = cur.fetchone()[0]
        self.assertTrue(cap["ok"]); self.assertFalse(cap["again"]); self.assertGreater(cap["pages"], 3)
        cur.execute("select doc_type, url, text_status, pages, label like 'Wages policy (web page, %%' from public.documents where library_id = %s", (cap["library_id"],))
        self.assertEqual(cur.fetchone(), ("Web page", "https://example.com/policies/wages", "ok", cap["pages"], True))
        cur.execute("select count(*), max(length(body)), bool_and(words is not null and doc_type = 'Web page' and wba_id = 'PT_00000') from public.pages where library_id = %s", (cap["library_id"],))
        n, longest, indexed = cur.fetchone(); self.assertEqual(n, cap["pages"]); self.assertLessEqual(longest, 6000); self.assertTrue(indexed)
        cur.execute("select public.page_capture('PT_00000', 'https://example.com/policies/wages', 'Wages policy', %s)", (text,)); self.assertTrue(cur.fetchone()[0]["same"])
        cur.execute("select public.page_capture('PT_00000', 'https://example.com/policies/wages', 'Wages policy', %s)", (text + "\n\nA new paragraph.",)); again = cur.fetchone()[0]
        self.assertTrue(again["again"]); self.assertEqual(again["library_id"], cap["library_id"])
        cur.execute("select count(*) from public.documents where url = 'https://example.com/policies/wages'"); self.assertEqual(cur.fetchone()[0], 1)   # one document an address
        for bad in (("PT_nope", "https://example.com/p", "t", text), ("PT_00000", "https://example.com/a.pdf", "t", text), ("PT_00000", "https://example.com/p", "t", "too short"), ("PT_00000", "ftp://example.com/p", "t", text)):
            with self.assertRaises(psycopg.Error): cur.execute("select public.page_capture(%s, %s, %s, %s)", bad)
        cur.execute("delete from public.pages where library_id = %s", (cap["library_id"],)); cur.execute("delete from public.documents where library_id = %s", (cap["library_id"],)); cur.execute("delete from public.web_captures")
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

    def test_a_contents_page_and_a_gri_index_are_told_from_content_and_from_tables_of_figures(self):
        cur = self.conn.cursor()
        kind = lambda body: (cur.execute("select public.page_kind(%s)", (body,)), cur.fetchone()[0])[1]
        toc = "Contents " + " ".join(f"Chapter {w} {'.' * 40} {n}" for n, w in enumerate(("People", "Planet", "Water", "Waste", "Board", "Tax", "Annex"), 3)) + " End"
        self.assertEqual(kind(toc), "contents")
        self.assertEqual(kind("Index Our people . . . . . . . . . . 4 Our planet . . . . . . . . 9 Our board . . . . . . . 12 Annex"), "contents")     # three leaders on a short page
        statement = " ".join(f"{w} {'.' * 30} {v} {v2}" for w, v, v2 in (("Revenue", "800,864", "711,146"), ("Cost of goods sold", "533,996", "510,455"),
                             ("Gross profit", "266,868", "200,691"), ("Expenses", "19.5", "19.3"), ("Tax", "(34.4)", "91.8"), ("Net", "$ 12", "$ 9"), ("Units", "14,414", "14,557")))
        self.assertIsNone(kind(statement))                                   # leaders followed by figures: a statement, never a contents page
        gri = " ".join(f"GRI {c} Some disclosure title {n}" for n, c in enumerate(("302-1", "302-3", "303-3", "304-1", "304-2", "305-1", "305-2", "305-3", "403-9"), 40))
        self.assertEqual(kind(gri), "index")
        self.assertIsNone(kind("Water withdrawals (303-3) by source: sea water 7,885.2 8,869.2 8,743.3; total 10,914.4 13,727.7. " * 3 + "x" * 3000))   # a table that names a disclosure
        self.assertIsNone(kind(" ".join(f"Seoul district {a} 887 268 788" for a in ("650-65", "164-2", "236-1", "219-43", "209-6", "127-91", "152-3", "131-9", "145-2"))))  # addresses
        self.assertIsNone(kind("CAS 127-91-3 / 201-291-9 / 204-872-5 beta-pinene " * 6))                       # numbers of three parts
        self.assertIsNone(kind("See pages 108-110, 203-205 and 301-305 of the report. " * 6))                    # ranges of pages
        self.assertIsNone(kind("We respect the opinions of others and pay a living wage in every country."))
        self.assertIsNone(kind(None))

    def test_the_documents_are_read_once_and_marked_pages_go_down_only_with_the_option(self):
        import psycopg
        toc = "Contents living wage " + " ".join(f"Living wage chapter {'.' * 40} {n}" for n in range(3, 11)) + " End"
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("select library_id, page, rank from public.find_pages('', 'living wage')")
            words = cur.fetchall()
            first = words[0]
            cur.execute("update public.documents set text_status = 'ok'")
            cur.execute("select body from public.pages where library_id = %s and page = %s", (first[0], first[1]))
            cur.execute("update public.pages set body = %s || ' ' || body where library_id = %s and page = %s", (toc, first[0], first[1]))
            cur.execute("select library_id, page from public.find_pages('', 'living wage')")
            before = cur.fetchall()
            self.assertGreater(len(before), 1)
            marked = before[0]
            cur.execute("select count(*) from public.documents where text_status = 'ok'")
            docs = cur.fetchone()[0]
            cur.execute("select public.page_kinds_step(2000)")
            step = cur.fetchone()[0]
            self.assertEqual(step["documents"], docs)
            self.assertGreaterEqual(step["marked"], 1)
            cur.execute("select kind from public.page_kinds where library_id = %s and page = %s", (first[0], first[1]))
            self.assertEqual(cur.fetchone()[0], "contents")
            cur.execute("select public.page_kinds_step(2000)")
            self.assertEqual(cur.fetchone()[0]["documents"], 0)                # read once
            cur.execute("select public.page_kinds_state()")
            state = cur.fetchone()[0]
            self.assertEqual((state["read"], state["documents"]), (docs, docs))
            self.assertGreaterEqual(state["kinds"]["contents"], 1)
            cur.execute("select set_config('lantern.page_kinds', 'off', false)")
            cur.execute("select library_id, page from public.find_blend('', 'living wage')")
            self.assertEqual(cur.fetchall(), before)                          # off: nothing moves
            cur.execute("select set_config('lantern.page_kinds', 'on', false)")
            cur.execute("select library_id, page, rank from public.find_blend('', 'living wage')")
            on = cur.fetchall()
            self.assertEqual({(r[0], r[1]) for r in on}, set(before))         # on: the same pages, none taken out
            self.assertEqual((on[-1][0], on[-1][1]), (first[0], first[1]))    # the contents page last
            self.assertEqual([r[2] for r in on], list(range(1, len(on) + 1)))
            # both lists merged: the marked page, first by words and first by meaning, still goes after a page only the words found
            cur.execute(psycopg.sql.SQL("""create or replace function public.find_meaning(key text, qvec real[], company_filter text default null, year_from int default null, year_to int default null, type_filter text default null, max_rows int default 50)
                           returns table (log_id bigint, rank int, library_id text, company text, wba_id text, year int, doc_type text, label text, url text, page int, score real, excerpt text)
                           language sql as $f$ select null::bigint, 40, {}::text, 'c'::text, 'w'::text, 2024, 't'::text, 'l'::text, null::text, {}::int, 0.9::real, 'nearest passage'::text $f$""").format(
                               psycopg.sql.Literal(first[0]), psycopg.sql.Literal(first[1])))
            cur.execute("select library_id, page from public.find_blend('', 'living wage', %s::real[])", ([0.0] * 384,))
            blended = cur.fetchall()
            self.assertEqual(set(blended), set(before))
            self.assertNotEqual(blended[0], (first[0], first[1]))
            cur.execute("select set_config('lantern.page_kinds', 'off', false)")
            cur.execute("select library_id, page from public.find_blend('', 'living wage', %s::real[])", ([0.0] * 384,))
            self.assertEqual(cur.fetchall()[0], (first[0], first[1]))          # off: found by both, it is first
            # a rule that no longer marks the page: the row stays, as content, and the search treats it as no mark
            cur.execute("update public.pages set body = 'living wage' where library_id = %s and page = %s", (first[0], first[1]))
            cur.execute("update public.page_kind_docs set rule = 0")
            cur.execute("select public.page_kinds_step(2000)")
            cur.execute("select kind from public.page_kinds where library_id = %s and page = %s", (first[0], first[1]))
            self.assertEqual(cur.fetchone()[0], "content")
            c.rollback()

    def test_the_metric_names_the_page_compares_are_the_taxonomy_s_and_the_exams_hold_the_same(self):
        import json, psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("select public.find_metric_names('k')"); names = cur.fetchone()[0]
            self.assertTrue({"ceo", "headquarters", "number employees", "scope 1 emissions", "total assets", "revenue"} <= set(names))
            self.assertFalse(any(len(n) < 3 for n in names))
            here = os.path.dirname(__file__)
            for f in ("use_cases.json", "use_case_variants.json"):                # the exams run the page on the names the database gives
                self.assertEqual(sorted(json.load(open(os.path.join(here, f), encoding="utf-8"))["metrics"]), sorted(names), f)
            cur.execute("update public.fact_metrics set status = 'retired' where key = 'ceo'")
            cur.execute("select public.find_metric_names('k')")
            self.assertNotIn("chief executive officer", cur.fetchone()[0])
            c.rollback()

    def test_the_jobs_view_marks_a_critical_job_that_failed_twice_in_a_row_or_is_late(self):
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            put = lambda i, wf, concl, hours, status="completed": cur.execute(
                "insert into public.job_runs (run_id, workflow, name, event, status, conclusion, started) values (%s, %s, %s, 'schedule', %s, %s, now() - make_interval(hours => %s))",
                (i, wf, wf.split(".")[0], status, concl, hours))
            put(1, "load_supabase.yml", "success", 50); put(2, "load_supabase.yml", "failure", 20); put(3, "load_supabase.yml", "failure", 5)     # twice in a row
            put(4, "load_supabase.yml", "cancelled", 3); put(5, "load_supabase.yml", None, 1, "in_progress")                                  # neither counts
            put(10, "propose_values.yml", "failure", 30); put(11, "propose_values.yml", "success", 6)                                         # failed once, then well
            put(20, "clock.yml", "success", 40)                                                                                                # late: 12 hours allowed
            put(30, "server_meaning.yml", "success", 30); put(31, "server_meaning.yml", "failure", 4)                                         # failed once: no mark yet
            put(40, "some_trial.yml", "failure", 9); put(41, "some_trial.yml", "failure", 8)                                                  # not critical: listed, no mark
            # a watch: the last finished one started 14 hours ago, one is running since 3 hours ago, the next waits its turn
            put(50, "scan_now.yml", "success", 14); put(51, "scan_now.yml", None, 3, "in_progress"); put(52, "scan_now.yml", None, 1, "pending"); put(53, "scan_now.yml", "cancelled", 2)
            cur.execute("select public.admin_jobs('k')"); r = cur.fetchone()[0]
            jobs = {j["workflow"]: j for j in r["jobs"]}
            self.assertEqual((jobs["scan_now.yml"]["late"], jobs["scan_now.yml"]["last_result"]), (False, "in_progress"))                    # running: not late, and the queued run is not "the last run"
            self.assertFalse(r["stale"])
            self.assertEqual((jobs["load_supabase.yml"]["in_a_row"], jobs["load_supabase.yml"]["failing"], jobs["load_supabase.yml"]["last_result"]), (2, True, "in_progress"))
            self.assertEqual((jobs["propose_values.yml"]["in_a_row"], jobs["propose_values.yml"]["failing"], jobs["propose_values.yml"]["late"]), (0, False, False))
            self.assertEqual((jobs["clock.yml"]["failing"], jobs["clock.yml"]["late"]), (False, True))
            self.assertEqual((jobs["server_meaning.yml"]["in_a_row"], jobs["server_meaning.yml"]["failing"], jobs["server_meaning.yml"]["late"]), (1, False, False))
            self.assertEqual((jobs["some_trial.yml"]["in_a_row"], jobs["some_trial.yml"]["critical"], jobs["some_trial.yml"]["failing"]), (2, False, False))
            self.assertTrue(jobs["library_mirror.yml"]["late"] and jobs["library_mirror.yml"]["last_started"] is None)                         # critical and never seen
            self.assertEqual(r["alerts"], sum(1 for j in r["jobs"] if j["failing"] or j["late"]))
            self.assertTrue(r["jobs"][0]["failing"] or r["jobs"][0]["late"])                                                                   # what needs a look comes first
            self.assertEqual((jobs["load_supabase.yml"]["runs_7d"], jobs["load_supabase.yml"]["failed_7d"]), (5, 2))
            c.rollback()

    def test_the_two_lists_the_page_asks_for_put_marked_pages_last_and_are_the_same_rows(self):
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("select library_id, page from public.find_pages('', 'living wage')"); plain = cur.fetchall()
            self.assertGreater(len(plain), 1)
            first = plain[0]
            cur.execute("insert into public.page_kinds (library_id, page, kind) values (%s, %s, 'index')", first)
            cur.execute("select set_config('lantern.page_kinds', 'off', false)")
            cur.execute("select library_id, page, rank from public.find_pages_ranked('', 'living wage')")
            self.assertEqual([(r[0], r[1]) for r in cur.fetchall()], plain)                 # off: find_pages, untouched
            cur.execute("select set_config('lantern.page_kinds', 'on', false)")
            cur.execute("select library_id, page, rank from public.find_pages_ranked('', 'living wage')"); on = cur.fetchall()
            self.assertEqual({(r[0], r[1]) for r in on}, set(plain)); self.assertEqual((on[-1][0], on[-1][1]), first)
            self.assertEqual([r[2] for r in on], list(range(1, len(on) + 1)))
            self.assertEqual([(r[0], r[1]) for r in on[:-1]], plain[1:])                    # the others keep their order
            cur.execute("update public.page_kinds set kind = 'content'")                    # a mark taken back counts as none
            cur.execute("select library_id, page from public.find_pages_ranked('', 'living wage')"); self.assertEqual(cur.fetchall(), plain)
            # the meaning: the same, over whatever find_meaning returns
            cur.execute("update public.page_kinds set kind = 'contents'")
            other = plain[1]
            cur.execute(psycopg.sql.SQL("""create or replace function public.find_meaning(key text, qvec real[], company_filter text default null, year_from int default null, year_to int default null, type_filter text default null, max_rows int default 50)
                           returns table (log_id bigint, rank int, library_id text, company text, wba_id text, year int, doc_type text, label text, url text, page int, score real, excerpt text)
                           language sql as $f$ select null::bigint, v.r, v.l, 'c'::text, 'w'::text, 2024, 't'::text, 'l'::text, null::text, v.p, 0.9::real, 'passage'::text
                                               from (values (1, {}::text, {}::int), (2, {}::text, {}::int)) v(r, l, p) $f$""").format(
                               *[psycopg.sql.Literal(x) for x in (first[0], first[1], other[0], other[1])]))
            cur.execute("select library_id, page, rank from public.find_meaning_ranked('', %s::real[])", ([0.0] * 384,))
            self.assertEqual(cur.fetchall(), [(other[0], other[1], 1), (first[0], first[1], 2)])
            cur.execute("select set_config('lantern.page_kinds', 'off', false)")
            cur.execute("select library_id, page, rank from public.find_meaning_ranked('', %s::real[])", ([0.0] * 384,))
            self.assertEqual(cur.fetchall(), [(first[0], first[1], 1), (other[0], other[1], 2)])
            c.rollback()

    def test_the_search_words_of_an_answer_are_words_not_the_models_talk(self):
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            clean = lambda t: (cur.execute("select public.ask_keywords_clean(%s)", (t,)), cur.fetchone()[0])[1]
            self.assertEqual(clean("Scope 3 use of sold products"), "scope 3 use of sold products")
            self.assertEqual(clean("scope 3 category 11 use of sold products wait the rule says write abbreviations in full scope 3 cat 11 should be c"), "scope 3 category 11 use of sold products")   # the owner's screenshot
            self.assertEqual(clean("The question is about emissions of the products sold by the company.\nscope 3 use of sold products"), "scope 3 use of sold products")   # thinking first, the words last
            self.assertEqual(clean("one two three four five six seven eight nine ten"), "one two three four five six seven eight")
            self.assertIsNone(clean("")); self.assertIsNone(clean("  \n ")); self.assertEqual(clean("living-wage, suppliers!"), "living wage suppliers")
            c.rollback()

    def test_the_owner_queues_documents_for_the_extension(self):
        """24 Nov 2026: rows of Documents to fetch are ticked and sent to a queue; the extension, signed in with the same
        address, reads what waits, downloads each with the owner's browser and says what came of it."""
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            for lid, url, wba in (("Q1", "https://x.example/a.pdf", "WA"), ("Q2", "https://x.example/b.pdf", "WA"), ("Q3", "", "WA"), ("Q4", "https://y.example/c.pdf", "")):
                cur.execute("insert into public.documents (library_id, wba_id, company, year, doc_type, label, url, text_status) values (%s, %s, 'Alpha', 2024, 'Annual Report', %s, %s, 'timeout')", (lid, wba, lid, url))
            cur.execute("select public.admin_fetch_queue_set('k', array['Q1', 'Q2', 'Q3', 'Q4', 'NOPE'])"); q = cur.fetchone()[0]
            self.assertEqual((q["waiting"], sorted(q["ids"])), (3, ["Q1", "Q2", "Q4"]))                         # a row with no address, or not in the library, is not queued
            cur.execute("select public.fetch_queue_mine()"); mine = cur.fetchone()[0]
            self.assertEqual([(m["library_id"], m["url"], m["wba_id"], m["year"], m["doc_type"]) for m in mine],
                             [("Q1", "https://x.example/a.pdf", "WA", 2024, "Annual Report"), ("Q2", "https://x.example/b.pdf", "WA", 2024, "Annual Report")])   # no company: nothing to send it for
            cur.execute("select public.fetch_queue_done('Q1', %s)", ("sent (1.2 MB)" + "x" * 400,)); self.assertTrue(cur.fetchone()[0]["ok"])
            cur.execute("select public.fetch_queue_done('NOPE', 'x')"); self.assertFalse(cur.fetchone()[0]["ok"])
            cur.execute("select public.admin_fetch_queue_set('k')"); q = cur.fetchone()[0]
            self.assertEqual((q["waiting"], sorted(q["ids"]), len(q["tried"]["Q1"]["result"]), q["tried"]["Q1"]["at"] is not None), (2, ["Q2", "Q4"], 200, True))
            cur.execute("select public.admin_fetch_queue_set('k', array['Q2', 'Q1'], false)"); q = cur.fetchone()[0]     # taken out; what was tried stays as a record
            self.assertEqual((q["waiting"], list(q["tried"])), (1, ["Q1"]))
            cur.execute("select public.admin_fetch_queue_set('k', array['Q1'])"); self.assertEqual(sorted(cur.fetchone()[0]["ids"]), ["Q1", "Q4"])   # queued again: it waits again
            c.rollback()
            cur.execute("create or replace function auth.jwt() returns jsonb language sql as $f$ select '{}'::jsonb $f$")           # nobody signed in; undone by the rollback
            with self.assertRaises(psycopg.Error):
                cur.execute("select public.fetch_queue_mine()")
            c.rollback()

    def test_the_documents_to_fetch_by_hand_are_listed_by_company_main_reports_first(self):
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            put = lambda lid, wba, company, year, typ, status, url="https://x.example/a.pdf", label=None: cur.execute(
                "insert into public.documents (library_id, wba_id, company, year, doc_type, label, url, text_status) values (%s, %s, %s, %s, %s, %s, %s, %s)",
                (lid, wba, company, year, typ, label or lid, url, status))
            put("U1", "WA", "Alpha", 2024, "Annual Report", "no_file"); put("U2", "WA", "Alpha", 2023, "Sustainability Report", "download_fail")
            put("U3", "WA", "Alpha", 2024, "Press Release", "no_file"); put("U4", "WA", "Alpha", 2021, "Annual Report", "no_file")
            put("U5", "WB", "Beta", 2025, "Integrated Report", "no_file", label="Beta integrated 2025"); put("U6", "WB", "Beta", 2024, "Annual Report", "ok")
            put("U7", "WB", "Beta", 2024, "Annual Report", "no_file", url="")                                   # no address: nothing to open
            cur.execute("select public.admin_unread_docs('k')"); r = cur.fetchone()[0]
            self.assertEqual((r["documents"], r["companies"]), (3, 2))                                          # main reports of 2023 on
            self.assertEqual([d["library_id"] for d in r["rows"]], ["U1", "U2", "U5"])                          # the company with most first, newest first
            self.assertEqual(r["rows"][0]["company_n"], 2); self.assertEqual(r["by_status"], {"no_file": 2, "download_fail": 1})
            self.assertEqual(r["rows"][0]["url"], "https://x.example/a.pdf")
            self.assertEqual((r["rows"][0]["note"], r["rows"][0]["traits"], r["rows"][0]["sent"]), (None, [], None))
            cur.execute("insert into public.company_notes (wba_id, traits, note, by) values ('WA', '{no_annual}', 'reports sit behind a form', 'a@x.org')")
            cur.execute("insert into public.browser_uploads (uploader, wba_id, source_url, filename, bytes, sha256, path, status) values ('a@x.org', 'WA', 'https://x.example/a.pdf', 'a.pdf', 10, 'h', 'WA/h.pdf', 'stored')")
            cur.execute("select public.admin_unread_docs('k')"); r2 = cur.fetchone()[0]
            self.assertEqual((r2["rows"][0]["note"], r2["rows"][0]["traits"], r2["rows"][0]["sent"]["status"]), ("reports sit behind a form", ["no_annual"], "stored"))
            self.assertEqual((r2["rows"][0]["company_sent"]["n"], r2["rows"][0]["company_sent"]["waiting"], r2["rows"][2]["company_sent"]), (1, 1, None))   # 22 Nov 2026: what was sent for the company, at once
            self.assertIsNone(r2["rows"][2]["sent"])                                                            # another company, same address: not its sending
            # which company first: one that lacks the main reports of both last years, then the one in more benchmarks
            import datetime; y = datetime.date.today().year - 1
            put("U8", "WC", "Gamma", y, "Annual Report", "no_file"); put("U9", "WC", "Gamma", y, "Annual Report", "ok", url=""); put("U10", "WC", "Gamma", y - 1, "Annual Report", "ok", url="")
            put("U11", "WD", "Delta", y, "Annual Report", "no_file")
            cur.execute("insert into public.fx_rates (code, per_usd, as_of) values ('USD', 1, current_date) on conflict do nothing")
            cur.execute("insert into public.value_versions (version_id, wba_id, attribute, year, value, unit, printed, library_id, page, method) values ('vWD', 'WD', 'GMC_00048.1', %s, 5e9, 'USD', 'x', 'L', 1, 'm')", (y - 2,))
            cur.execute("select public.admin_unread_docs('k')"); r3 = cur.fetchone()[0]; seen = []
            for d in r3["rows"]:
                if d["wba_id"] not in seen: seen.append(d["wba_id"])
            self.assertEqual(seen[0], "WD"); self.assertNotIn("WC", seen); self.assertEqual(r3["covered"], 1)      # nothing read and the largest revenue first; an address whose year and kind the library already reads is counted, not listed (22 Nov 2026)
            first = r3["rows"][0]; self.assertEqual((first["revenue_usd"], first["missing"]), (5e9, f"no main report read for {y} or {y - 1}"))
            self.assertNotIn("benchmarks", first)                                                               # the order does not rest on WBA's benchmarks
            cur.execute("insert into public.company_notes (wba_id, traits, by) values ('WD', '{in_parent}', 'a@x.org')")
            cur.execute("select public.admin_unread_docs('k')"); r4 = cur.fetchone()[0]
            self.assertNotEqual(r4["rows"][0]["wba_id"], "WD")                                                  # ticked as reporting inside its parent: not first any more
            # the owner's corrections: a discarded document leaves the list at once, a corrected one shows its new type and year
            cur.execute("select public.admin_doc_fix('k', 'U11', 'discard', null, null, 'Dead link')")
            cur.execute("select public.admin_doc_fix('k', 'U8', 'relabel', 'CSR Report', %s, null)", (y,))
            cur.execute("select public.admin_unread_docs('k')"); r5 = cur.fetchone()[0]; ids5 = {d["library_id"]: d for d in r5["rows"]}
            self.assertNotIn("U11", ids5); self.assertEqual((ids5["U8"]["doc_type"], ids5["U8"]["corrected"]), ("CSR Report", True))
            cur.execute("select action, reason, doc_type, applied_at from public.doc_fixes order by id"); self.assertEqual(cur.fetchall(), [("discard", "dead link", None, None), ("relabel", None, "CSR Report", None)])
            for bad in ("select public.admin_doc_fix('k', 'NOPE', 'discard')", "select public.admin_doc_fix('k', 'U8', 'delete')", "select public.admin_doc_fix('k', 'U8', 'relabel')", "select public.admin_doc_fix('k', 'U8', 'discard', null, null, 'whim')"):
                cur.execute("savepoint s")
                with self.assertRaises(psycopg.Error): cur.execute(bad)
                cur.execute("rollback to savepoint s")
            cur.execute("delete from public.doc_fixes")
            # "do we hold the latest?" (v125): the year due from the fiscal year's end, and the companies with no main report of it
            due = lambda m, day: (cur.execute("select public.report_year_due(%s, %s::date)", (m, day)), cur.fetchone()[0])[1]
            self.assertEqual([due(12, "2026-11-19"), due(None, "2026-11-19"), due(3, "2026-11-19"), due(9, "2026-11-19")], [2025, 2025, 2025, 2025])
            self.assertEqual([due(12, "2026-07-31"), due(12, "2026-08-01"), due(3, "2026-10-31"), due(6, "2027-02-01")], [2024, 2025, 2024, 2026])
            cur.execute("select public.report_year_due(null)"); dy = cur.fetchone()[0]
            cur.execute("insert into public.companies (wba_id, name, website) values ('LZ1', 'Zeta One', 'https://one.example'), ('LZ2', 'Zeta Two', null), ('LZ3', 'Zeta Three', null), ('LZ4', 'Zeta Four', null)")
            put("Z2", "LZ2", "Zeta Two", dy, "Annual Report", "no_file"); put("Z3", "LZ3", "Zeta Three", dy - 1, "Sustainability Report", "ok", url="https://x.example/z3.pdf")
            put("Z3b", "LZ3", "Zeta Three", dy, "Policy Documents", "ok"); put("Z4", "LZ4", "Zeta Four", dy - 2, "Annual Report", "ok")
            cur.execute("insert into public.company_notes (wba_id, traits, by) values ('LZ4', '{in_parent}', 'a@x.org')")
            cur.execute("select public.admin_latest_missing('k', 'Zeta')"); lm = cur.fetchone()[0]
            self.assertEqual((lm["companies"], lm["with_none"]), (3, 1))                                        # a held address of the year due is not missing here; a policy is not a main report
            self.assertEqual([x["wba_id"] for x in lm["rows"]][-1], "LZ4")                                      # reports inside its parent: last
            z3 = [x for x in lm["rows"] if x["wba_id"] == "LZ3"][0]
            self.assertEqual((z3["due"], z3["latest"], z3["last"]["url"], z3["last"]["doc_type"]), (dy, dy - 1, "https://x.example/z3.pdf", "Sustainability Report"))
            z1 = [x for x in lm["rows"] if x["wba_id"] == "LZ1"][0]; self.assertEqual((z1["latest"], z1["last"], z1["website"]), (None, None, "https://one.example"))
            cur.execute("delete from public.documents where library_id in ('Z2', 'Z3', 'Z3b', 'Z4')"); cur.execute("delete from public.companies where wba_id like 'LZ%'"); cur.execute("delete from public.company_notes where wba_id = 'LZ4'")
            cur.execute("delete from public.documents where library_id in ('U8', 'U9', 'U10', 'U11')")           # (rows of this test only; rolled back at the end)
            cur.execute("select public.admin_unread_docs('k', null, false, null)"); r = cur.fetchone()[0]
            self.assertEqual(r["documents"], 5)                                                                 # every kind, every year
            self.assertEqual([d["library_id"] for d in r["rows"]][:4], ["U1", "U2", "U4", "U3"])                # main reports before the rest
            cur.execute("select public.admin_unread_docs('k', 'integrated')"); r = cur.fetchone()[0]
            self.assertEqual([d["library_id"] for d in r["rows"]], ["U5"])
            cur.execute("select public.admin_unread_docs('k', 'alph', true, 2020)"); self.assertEqual(cur.fetchone()[0]["documents"], 3)
            c.rollback()

    def test_an_answer_is_kept_and_given_again_only_for_the_same_question_of_the_same_company(self):
        import psycopg, math
        vec = lambda *head: [float(x) for x in head] + [0.0] * (384 - len(head))
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("select public.cosine(%s::real[], %s::real[]), public.cosine(%s::real[], %s::real[]), public.cosine(%s::real[], %s::real[])",
                        ([1, 0], [1, 0], [1, 0], [0, 1], [0, 0], [1, 0]))
            same, apart, none = cur.fetchone()
            self.assertAlmostEqual(same, 1.0); self.assertAlmostEqual(apart, 0.0); self.assertIsNone(none)
            cur.execute("select public.ask_words_norm('  Scope 1  emissions?! '), public.ask_nums('scope 2 and scope 1, 2 again'), public.ask_nums('water')")
            self.assertEqual(cur.fetchone(), ("scope 1 emissions", "1 2", ""))
            reply = {"answer": "Scope 1 was 10 [1].", "sources": [{"n": 1}], "facts": []}
            cur.execute("select public.ask_keep(7, 'W1', 2023, 2023, null, 'Scope 1 emissions', %s::real[], %s::jsonb)", (vec(1, 0.1), psycopg.types.json.Jsonb(reply)))
            ask = lambda company, y0, y1, kind, q, v: (cur.execute("select public.ask_reusable(%s, %s, %s, %s, %s, %s::real[])", (company, y0, y1, kind, q, v)), cur.fetchone()[0])[1]
            hit = ask("W1", 2023, 2023, None, "scope 1 EMISSIONS", None)
            self.assertEqual((hit["ask_id"], hit["reply"]["answer"], hit["same"]), (7, "Scope 1 was 10 [1].", True))         # the same words, no vector needed
            near = ask("W1", 2023, 2023, None, "what are the scope 1 emissions", vec(1, 0.12))
            self.assertEqual((near["ask_id"], near["same"], near["words"]), (7, False, "scope 1 emissions"))                    # other words that mean the same
            self.assertIsNone(ask("W1", 2023, 2023, None, "scope 2 emissions", vec(1, 0.1)))                                    # the same vector, another number
            self.assertIsNone(ask("W1", 2023, 2023, None, "what are the scope 1 targets", vec(1, 0.9)))                          # the meaning is not near enough
            self.assertIsNone(ask("W1", 2023, 2023, None, "what are the scope 1 emissions", None))                               # other words and no vector
            self.assertIsNone(ask("W2", 2023, 2023, None, "scope 1 emissions", vec(1, 0.1)))                                    # another company
            self.assertIsNone(ask("W1", 2024, 2024, None, "scope 1 emissions", vec(1, 0.1)))                                    # other years
            self.assertIsNone(ask("W1", None, None, None, "scope 1 emissions", vec(1, 0.1)))
            self.assertIsNone(ask("W1", 2023, 2023, "Annual Report", "scope 1 emissions", vec(1, 0.1)))                         # another kind of report
            # the page asked before the vector was ready: the one made since for the same words is used, on both sides
            self.assertEqual(ask("W1", 2023, 2023, None, "scope 1 emissio ns", None)["ask_id"], 7)                             # a stray space: the same words
            cur.execute("select public.ask_keep(8, 'W1', null, null, null, 'supply chain', null, %s::jsonb)", (psycopg.types.json.Jsonb(reply),))
            self.assertIsNone(ask("W1", None, None, None, "supply chains", vec(0.2, 1)))                                        # nothing to compare with yet
            cur.execute("insert into public.query_vectors (q, vec) values ('Supply chain', %s::real[]), ('supply chains', %s::real[])", (vec(0.21, 1), vec(0.2, 1)))
            self.assertEqual(ask("W1", None, None, None, "supply chains", vec(0.2, 1))["ask_id"], 8)                            # the kept answer's vector, made since
            self.assertEqual(ask("W1", None, None, None, "supply chains", None)["ask_id"], 8)                                   # and the question's own, when the page sent none
            cur.execute("select public.ask_keep(9, 'W3', null, null, null, 'Supply chain', null, %s::jsonb)", (psycopg.types.json.Jsonb(reply),))
            cur.execute("select vec is not null from public.ask_cache where ask_id = 9"); self.assertTrue(cur.fetchone()[0])     # kept with the vector already made
            cur.execute("delete from public.ask_cache where ask_id in (8, 9)")
            cur.execute("update public.ask_cache set at = now() - interval '15 days'")
            self.assertIsNone(ask("W1", 2023, 2023, None, "scope 1 emissions", vec(1, 0.1)))                                    # too old: documents arrive
            cur.execute("update public.ask_cache set at = now()")
            # the library moved (22 Nov 2026): a document with text arrives for the company, the kept answer is not given; it leaves, it is
            cur.execute("select library, public.ask_library_state('W1') from public.ask_cache where ask_id = 7"); was, now_ = cur.fetchone()
            self.assertEqual(was, now_); self.assertRegex(was, r"^\d+:\d+:\d+$")
            cur.execute("insert into public.documents (library_id, wba_id, company, doc_type, year, pages, text_status) values ('L-late', 'W1', 'Acme', 'Integrated Report', 2023, 250, 'ok')")
            self.assertIsNone(ask("W1", 2023, 2023, None, "scope 1 emissions", vec(1, 0.1)))
            cur.execute("delete from public.documents where library_id = 'L-late'")
            self.assertEqual(ask("W1", 2023, 2023, None, "scope 1 emissions", vec(1, 0.1))["ask_id"], 7)
            cur.execute("update public.ask_cache set library = null where ask_id = 7")                                          # kept before the rule: not given
            self.assertIsNone(ask("W1", 2023, 2023, None, "scope 1 emissions", vec(1, 0.1)))
            cur.execute("update public.ask_cache set library = public.ask_library_state('W1') where ask_id = 7")
            # "Not stated" is never given again
            cur.execute("select public.ask_keep(10, 'W1', 2023, 2023, null, 'water withdrawal', null, %s::jsonb)", (psycopg.types.json.Jsonb({"answer": "Not stated in these pages. They cover governance.", "sources": []}),))
            self.assertIsNone(ask("W1", 2023, 2023, None, "water withdrawal", None))
            cur.execute("delete from public.ask_cache where ask_id = 10")
            # through ask itself: given again before anything is counted or paid, logged as reused, never as an answer
            cur.execute("select public.ask('k', 'Acme scope 1 emissions 2023', 'scope 1 emissions', 'W1', 2023, 2023)"); got = cur.fetchone()[0]
            self.assertEqual((got["answer"], got["reused"]["same"], got["left"], got["per_day"]), ("Scope 1 was 10 [1].", True, got["per_day"], got["per_day"]))
            cur.execute("select answered, reused from public.ask_log order by id desc limit 1"); self.assertEqual(cur.fetchone(), (False, 7))
            # a follow-up (page v124): the answer says which it is; it may be followed by the person it was written for or given again to
            self.assertEqual(got["ask_id"], 7)
            cur.execute("select asker from public.ask_log order by id desc limit 1"); me = cur.fetchone()[0]
            cur.execute("insert into public.ask_log (id, asker, question, answered, answer, sources) overriding system value values (7, 'first@x.org', 'scope 1?', true, 'Scope 1 was 10 [1].', %s::jsonb) on conflict (id) do update set answered = true, answer = excluded.answer, sources = excluded.sources", (psycopg.types.json.Jsonb(reply["sources"]),))
            cur.execute("select public.ask_followable(7, %s), public.ask_followable(7, 'stranger@x.org')", (me,)); mine, theirs = cur.fetchone()
            self.assertEqual(mine["answer"], "Scope 1 was 10 [1]."); self.assertIsNone(theirs)
            cur.execute("select public.ask_follow('k', 7, 'and in 2022?')"); self.assertTrue(cur.fetchone()[0]["off"])            # no model key here: nothing asked, nothing counted
            cur.execute("insert into public.ask_log (asker, question, answered, answer, sources) values ('stranger@x.org', 'theirs?', true, 'x', %s::jsonb) returning id", (psycopg.types.json.Jsonb(reply["sources"]),)); other = cur.fetchone()[0]
            for bad in (("select public.ask_follow('k', %s, 'and in 2022?')", (other,)), ("select public.ask_follow('k', 7, 'x')", ())):
                cur.execute("savepoint s")
                with self.assertRaises(psycopg.Error): cur.execute(*bad)
                cur.execute("rollback to savepoint s")
            cur.execute("select public.ask_follow_text('What is scope 1?', 'It was 10 [1].', 'and in 2022?')"); ft = cur.fetchone()[0]
            self.assertTrue(ft.startswith("and in 2022?")); self.assertIn("Earlier question: What is scope 1?", ft); self.assertIn("Earlier answer: It was 10 [1].", ft)
            cur.execute("delete from public.ask_log where id = %s", (other,))
            cur.execute("select public.ask('k', 'Acme scope 1 emissions 2023', 'scope 1 emissions', 'W1', 2023, 2023, null, null, null, null, null, null, true)")
            self.assertNotIn("reused", cur.fetchone()[0])                                                                         # asked again: a new answer is sought
            cur.execute("select set_config('lantern.ask_reuse', 'off', false)")
            cur.execute("select public.ask('k', 'Acme scope 1 emissions 2023', 'scope 1 emissions', 'W1', 2023, 2023)"); self.assertNotIn("reused", cur.fetchone()[0])
            cur.execute("select set_config('lantern.ask_reuse', 'on', false)")
            cur.execute("select public.ask('k', 'Acme scope 1 emissions 2023', 'scope 1 emissions', 'W1;W2', 2023, 2023)"); self.assertNotIn("reused", cur.fetchone()[0])   # two companies: never
            cur.execute("select public.search_flags('k')"); self.assertEqual(cur.fetchone()[0], {"answer_topics": True, "ask_reuse": True})
            c.rollback()

    def test_in_the_list_by_meaning_older_documents_go_down_only_with_the_option_and_no_year(self):
        import psycopg, datetime
        y = datetime.date.today().year
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            rows = ", ".join(f"({r}, 'L{r}'::text, {yr}, {r})" for r, yr in ((1, 2001), (2, y - 4), (3, 0), (4, y - 1), (5, y - 1), (6, y - 2)))
            cur.execute(f"""create or replace function public.find_meaning(key text, qvec real[], company_filter text default null, year_from int default null, year_to int default null, type_filter text default null, max_rows int default 50)
                           returns table (log_id bigint, rank int, library_id text, company text, wba_id text, year int, doc_type text, label text, url text, page int, score real, excerpt text)
                           language sql as $f$ select null::bigint, v.r, v.l, 'c'::text, 'w'::text, v.y, 't'::text, 'l'::text, null::text, v.p, 0.9::real, 'passage'::text from (values {rows}) v(r, l, y, p) $f$""")
            ask = lambda *years: (cur.execute("select library_id from public.find_meaning_ranked('', %s::real[], null, %s, %s)", ([0.0] * 384, *years)), [r[0] for r in cur.fetchall()])[1]
            cur.execute("select set_config('lantern.page_kinds', 'off', false), set_config('lantern.recent_meaning', 'off', false)")
            self.assertEqual(ask(None, None), ["L1", "L2", "L3", "L4", "L5", "L6"])                    # off: nearness alone
            cur.execute("select set_config('lantern.recent_meaning', 'on', false)")
            # ages over the newest (a year old): 2001 capped at twelve -> +16.5; four years -> +4.5; no year (three) -> +3; two years -> +1.5
            self.assertEqual(ask(None, None), ["L4", "L5", "L3", "L2", "L6", "L1"])
            self.assertEqual(ask(2001, y), ["L1", "L2", "L3", "L4", "L5", "L6"])                       # a year asked: nothing moves
            cur.execute("insert into public.page_kinds (library_id, page, kind) values ('L4', 4, 'index')")
            cur.execute("select set_config('lantern.page_kinds', 'on', false)")
            self.assertEqual(ask(None, None), ["L5", "L3", "L2", "L6", "L1", "L4"])                    # a marked page still goes last
            cur.execute("select library_id, rank from public.find_meaning_ranked('', %s::real[])", ([0.0] * 384,))
            self.assertEqual([r[1] for r in cur.fetchall()], [1, 2, 3, 4, 5, 6])
            c.rollback()

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
        self.assertIn('rpc("find_meaning_ranked"', src); self.assertIn('rpc("find_pages_ranked"', src); self.assertIn("function blend(words, meaning, need)", src)   # the page merges the two itself, asked at once
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

    def test_the_server_is_asked_only_when_switched_on_and_a_breaker_guards_it(self):
        """meaning_remote (16 Nov 2026; on since 17 Nov): switched off, the server is never asked; three failures in a row and it is not asked for a minute."""
        import psycopg
        call = """create or replace function public.meaning_remote_call(qvec real[], ids text[], year_from int, year_to int, types text[], max_rows int default 400)
                  returns jsonb language plpgsql as $f$ begin perform nextval('public.calls_seen'); return current_setting('test.reply')::jsonb; end $f$"""
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("select public.meaning_remote_call(array[0.1]::real[], null, null, null, null)")
            self.assertEqual(cur.fetchone()[0]["counts"], False)                 # nothing set up here: an error that does not count against the server
            cur.execute("create sequence public.calls_seen"); cur.execute(call)
            rows = lambda: (cur.execute("select public.meaning_remote_rows(array[0.1]::real[], null, null, null, null)"), cur.fetchone()[0])[1]
            calls = lambda: (cur.execute("select last_value, is_called from public.calls_seen"), (lambda r: r[0] if r[1] else 0)(cur.fetchone()))[1]
            state = lambda: (cur.execute("select fails, skip_until is not null from public.meaning_remote_state"), cur.fetchone())[1]
            cur.execute("select set_config('test.reply', %s, true)", ('{"rows": [["L", 1, 0, 0.1]]}',))
            cur.execute("select is_on, note like '%%on since 17 Nov 2026%%' from public.search_options where name = 'meaning_remote'")
            self.assertEqual(cur.fetchone(), (True, True))                       # on since 17 Nov 2026, the owner's decision
            cur.execute("select set_config('lantern.meaning_remote', 'off', true)")
            self.assertIsNone(rows()); self.assertEqual(calls(), 0)              # the option is off: the server is never asked
            cur.execute("select set_config('lantern.meaning_remote', 'on', true)")
            self.assertEqual(rows(), [["L", 1, 0, 0.1]]); self.assertEqual((calls(), state()), (1, (0, False)))
            cur.execute("select set_config('test.reply', %s, true)", ('{"error": "HTTP 400", "counts": false}',))
            self.assertIsNone(rows()); self.assertEqual(state(), (0, False))     # this side's mistake: not held against the server
            cur.execute("select set_config('test.reply', %s, true)", ('{"error": "no answer", "counts": true}',))
            self.assertIsNone(rows()); self.assertIsNone(rows()); self.assertEqual(state(), (2, False))
            self.assertIsNone(rows()); self.assertEqual(state(), (3, True))      # the third in a row opens the breaker
            before = calls()
            self.assertIsNone(rows()); self.assertEqual(calls(), before)         # and now nobody waits for the server
            cur.execute("update public.meaning_remote_state set skip_until = now() - interval '1 second'")
            cur.execute("select set_config('test.reply', %s, true)", ('{"rows": []}',))
            self.assertEqual(rows(), []); self.assertEqual(state(), (0, False))  # the minute is over and it answers: closed again
            cur.execute("select asked, failed, skipped, used, too_little from public.meaning_remote_days where day = current_date")
            self.assertEqual(cur.fetchone(), (2, 4, 1, 0, 0))                    # counted as they happen: two answers; four without one (this side's mistake too); one pause
            cur.execute("select public.meaning_remote_count('used'), public.meaning_remote_count('x; drop')")
            cur.execute("select used from public.meaning_remote_days where day = current_date"); self.assertEqual(cur.fetchone()[0], 1)
            cur.execute("select has_function_privilege('anon', 'public.meaning_remote_call(real[], text[], int, int, text[], int)', 'execute'), "
                        "has_function_privilege('authenticated', 'public.meaning_remote_probe(int)', 'execute'), (select is_on from public.search_options where name = 'meaning_remote')")
            self.assertEqual(cur.fetchone(), (False, False, True))
            cur.execute("update public.search_options set is_on = false where name = 'meaning_remote'")      # as the nightly refresh does when the copies differ
            cur.execute("update public.search_options set is_on = true, changed = now() where name = 'meaning_remote' and not is_on and note not like '%%on since%%'")
            cur.execute("select is_on from public.search_options where name = 'meaning_remote'")
            self.assertFalse(cur.fetchone()[0])                                  # and an install does not switch it back on
            c.rollback()

    def test_the_servers_keys_become_pages_checked_against_this_database(self):
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("create table if not exists public.chunks (library_id text not null, page int not null, n int not null, body text not null, primary key (library_id, page, n))")
            for lib, wba, year, kind in (("RA", "W1", 2024, "Annual Report"), ("RB", "W2", 2023, "Policy"), ("RC", "W1", 2025, "Annual Report")):
                cur.execute("insert into public.documents (library_id, wba_id, company, doc_type, year) values (%s, %s, %s, %s, %s)", (lib, wba, wba, kind, year))
            cur.execute("insert into public.chunks (library_id, page, n, body) values ('RA', 1, 0, 'a one'), ('RA', 1, 1, 'a one b'), ('RA', 2, 0, 'a two'), ('RA', 3, 0, 'a three'), ('RA', 4, 0, 'a four'), ('RB', 9, 0, 'b nine')")
            remote = '[["RA",1,1,0.30],["RA",1,0,0.10],["RB",9,0,0.20],["RA",2,0,0.25],["RA",3,0,0.26],["RA",4,0,0.27],["GONE",1,0,0.01],["RC",1,0,0.02],"x",["RA",1]]'
            q = "select library_id, page, rank, excerpt, round(score::numeric, 2) from public.meaning_remote_pages(%s::jsonb, %s, %s, %s, %s, %s)"
            cur.execute(q, (remote, None, None, None, None, 10))
            got = cur.fetchall()
            # nearest first; one line a page (its best passage); three pages a document; a key this database does not hold is dropped
            self.assertEqual([(g[0], g[1], g[2]) for g in got], [("RA", 1, 1), ("RB", 9, 2), ("RA", 2, 3), ("RA", 3, 4)])
            self.assertEqual((got[0][3], float(got[0][4])), ("a one", 0.9))
            cur.execute(q, (remote, ["W1"], 2024, 2024, ["Annual Report"], 10))
            self.assertEqual({g[0] for g in cur.fetchall()}, {"RA"})             # company, year and type checked again here
            cur.execute(q, (remote, None, None, None, None, 2))
            self.assertEqual(len(cur.fetchall()), 2)
            cur.execute(q, (None, None, None, None, None, 5))
            self.assertEqual(cur.fetchall(), [])
            # a document whose passages are kept on the server only: no passage here, the page's own text is shown
            cur.execute("insert into public.pages (library_id, page, body) values ('RC', 1, 'the policy as its page prints it')")
            cur.execute(q, (remote, None, None, None, None, 10))
            got = cur.fetchall()
            self.assertEqual((got[0][0], got[0][1], got[0][3]), ("RC", 1, "the policy as its page prints it"))
            self.assertEqual([(g[0], g[1]) for g in got[1:]], [("RA", 1), ("RB", 9), ("RA", 2), ("RA", 3)])
            c.rollback()

    def test_a_survey_asks_each_company_and_counts_honestly(self):
        """Surveys (16 Nov 2026): off unless the owner gives a number; a filter is the scope; a reading a company; no page is not a no."""
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("create or replace function auth.jwt() returns jsonb language sql as $f$ select current_setting('test.jwt')::jsonb $f$")   # undone by the rollback
            jwt = lambda who: cur.execute("select set_config('test.jwt', %s, true)", ('{"email": "%s"}' % who,))
            jwt("ana@worldbenchmarkingalliance.org")
            cur.execute("create table if not exists public.companies (wba_id text primary key, name text, industry text, country text, region text, website text, lantern_id text)")
            for wba, name in (("S1", "Alpha"), ("S2", "Beta"), ("S3", "Gamma"), ("S4", "Delta"), ("S5", "No Documents"), ("S6", "Epsilon")):
                cur.execute("insert into public.companies (wba_id, name, industry) values (%s, %s, 'Testing industry') on conflict do nothing", (wba, name))
                if wba != "S5":
                    cur.execute("insert into public.documents (library_id, wba_id, company, doc_type, year) values (%s, %s, %s, 'Annual Report', 2024)", ("D" + wba, wba, name))
                    cur.execute("insert into public.pages (library_id, page, body) values (%s, 1, %s)", ("D" + wba, f"{name} page text, long enough to be found by its start"))
            # the search and the reader, stubbed: Delta has no page; Beta's reader fails; Gamma says not stated
            cur.execute("""create or replace function public.ask_pages(key text, q text, qvec real[], company_filter text, year_from int, year_to int, type_filter text, asked text, industry_filter text default null, country_filter text default null, region_filter text default null)
                           returns table (library_id text, company text, wba_id text, year int, doc_type text, label text, url text, page int, excerpt text) language sql as $f$
                             select 'D' || company_filter, 'x', company_filter, 2024, 'Annual Report', 'the report', 'http://x', g, 'e' from generate_series(1, 8) g where company_filter <> 'S4' $f$""")
            cur.execute("""create or replace function public.ask_model(api_key text, http_schema text, model text, system_text text, user_text text, max_tokens int) returns jsonb language sql as $f$
                             select case when user_text like '%Company: Alpha%' then jsonb_build_object('in', 4000, 'out', 300, 'text', 'Thinking. {"answer": "yes", "value": "10% of bonus", "evidence": "Ten per cent of the bonus depends on emissions.", "year": 2024, "source": 2}')
                                         when user_text like '%Company: Epsilon%' then jsonb_build_object('in', 4000, 'out', 300, 'text', '{"answer": "yes", "value": null, "evidence": "Epsilon  page text, long enough to be found by its start", "year": null, "source": 9}')
                                         when user_text like '%Company: Gamma%' and user_text like '%[7]%' then jsonb_build_object('in', 4000, 'out', 300, 'text', '{"answer": "yes", "value": "found later", "evidence": null, "year": null, "source": 7}')
                                         when user_text like '%Company: Gamma%' then jsonb_build_object('in', 4000, 'out', 200, 'text', '{"answer": "Not stated", "value": null, "evidence": null, "year": null, "source": null}')
                                         when user_text like '%search each company%' or system_text like '%words to search%' then jsonb_build_object('in', 10, 'out', 5, 'text', 'remuneration emissions incentive')
                                         else jsonb_build_object('error', 'no reply') end $f$""")
            cur.execute("create or replace function public.reader_ready() returns jsonb language sql as $f$ select '{\"api_key\": \"k\", \"http_schema\": \"h\"}'::jsonb $f$")
            start = "select public.survey_start('k', 'Do executives have an incentive tied to greenhouse gas emissions?', null, 'Testing industry')"
            cur.execute(start); self.assertTrue(cur.fetchone()[0]["off"])                              # nobody may until the owner says so
            offer = "select public.survey_offer('k', 'do executives have an incentive tied to greenhouse gas emissions?', 'Testing industry')"
            cur.execute(offer); self.assertEqual(cur.fetchone()[0], {"may": False, "why": "off", "have": None})   # and nothing is offered
            jwt("e.owner@worldbenchmarkingalliance.org")
            cur.execute("select public.admin_surveys('k', 6, 4, null, 'ana@worldbenchmarkingalliance.org', 1)")
            got = cur.fetchone()[0]
            self.assertEqual((got["settings"], got["people"][0]["per_day"]), ({"pages": 6, "max_companies": 4, "readings": 2000}, 1))
            cur.execute("select per_day from public.ask_limits where email = 'ana@worldbenchmarkingalliance.org'")
            self.assertEqual(cur.fetchone()[0], 50)                                                    # surveys alone leave the answers a day as they are
            jwt("ana@worldbenchmarkingalliance.org")
            cur.execute(offer); o = cur.fetchone()[0]                                                    # before anything is spent: how many, about how much
            self.assertEqual((o["may"], o["companies"], o["in_scope"], o["up_to"], o["pages"], o["left_today"], o["have"]), (True, 4, 5, False, 6, 1, None))
            self.assertGreater(float(o["usd"]), 0)
            cur.execute("select public.survey_offer('k', 'a question with no filter at all?')")
            wide = cur.fetchone()[0]
            self.assertEqual([wide[k] for k in ("companies", "up_to", "in_scope")], [4, True, None])    # no filter: the ceiling, not a count
            cur.execute(start); made = cur.fetchone()[0]
            cur.execute(offer); o = cur.fetchone()[0]
            self.assertEqual((o["may"], o["why"], o["have"]["id"], o["have"]["status"]), (False, "limit", made["id"], "open"))   # the survey already run is opened, not paid twice
            cur.execute("select public.survey_offer('k', 'Do executives have an incentive tied to greenhouse gas emissions?', 'Testing industry', null, null, 2024)")
            self.assertIsNone(cur.fetchone()[0]["have"])                                                 # another scope is another survey
            self.assertEqual((made["companies"], made["in_scope"], made["pages"]), (4, 5, 6))            # five with documents, the ceiling keeps four, by name
            cur.execute(start); self.assertTrue(cur.fetchone()[0]["limit"])                             # one a day was allowed
            cur.execute("select public.survey_get('k', %s)", (made["id"],))
            self.assertEqual([r["company"] for r in cur.fetchone()[0]["rows"]], ["Alpha", "Beta", "Delta", "Epsilon"])
            self.assertEqual(made["words"], "remuneration emissions incentive")                         # a question in full is searched with a few words
            cur.execute("select public.survey_step('k', %s, 2)", (made["id"],)); got = cur.fetchone()[0]
            self.assertEqual((got["read"], got["status"], got["counts"]["pending"]), (2, "open", 2))      # a few companies a call
            cur.execute("select public.survey_step('k', %s, 5)", (made["id"],)); got = cur.fetchone()[0]
            self.assertEqual((got["status"], got["counts"]["yes"], got["counts"]["failed"], got["counts"]["no_page"], got["counts"]["pending"]), ("done", 2, 1, 1, 0))
            alpha, beta, delta = (next(r for r in got["rows"] if r["company"] == n) for n in ("Alpha", "Beta", "Delta"))
            self.assertEqual((alpha["status"], alpha["value"], alpha["year"], alpha["library_id"], alpha["page"], alpha["label"]), ("yes", "10% of bonus", 2024, "DS1", 2, "the report"))
            self.assertEqual((beta["status"], delta["status"], delta["evidence"]), ("failed", "no_page", None))     # no page found is never a no
            eps = next(r for r in got["rows"] if r["company"] == "Epsilon")
            self.assertEqual((eps["status"], eps["library_id"], eps["page"]), ("yes", "DS6", 1))          # a page not among those given: the page that prints the sentence
            cur.execute("update public.pages set body = 'nothing of the kind' where library_id = 'DS6'")
            cur.execute("update public.survey_rows set status = 'pending' where survey_id = %s and wba_id = 'S6'", (made["id"],))
            cur.execute("select public.survey_step('k', %s, 1)", (made["id"],))
            eps = next(r for r in cur.fetchone()[0]["rows"] if r["company"] == "Epsilon")
            self.assertEqual((eps["status"], eps["library_id"], eps["page"]), ("yes", None, None))        # and no page at all rather than one chosen for it
            self.assertGreater(float(got["counts"]["usd"]), 0)
            cur.execute("select has_function_privilege('anon', 'public.survey_start(text, text, real[], text, text, text, int, int, text, text, text)', 'execute'), "
                        "has_function_privilege('authenticated', 'public.reader_ready()', 'execute'), "
                        "has_function_privilege('authenticated', 'public.survey_read(text, text, text, real[], text, text, int, int, int)', 'execute'), "
                        "has_function_privilege('authenticated', 'public.survey_words(text)', 'execute'), "
                        "has_function_privilege('anon', 'public.survey_offer(text, text, text, text, text, int, int, text, text)', 'execute')")
            self.assertEqual(cur.fetchone(), (False, False, False, False, False))
            cur.execute("update public.pages set body = 'Epsilon page text, long enough to be found by its start' where library_id = 'DS6'")
            # option survey_again: after "not stated", the next pages of the wider search are read, numbered on from the first
            cur.execute("""create or replace function public.find_blend(key text, q text, qvec real[] default null, company_filter text default null, year_from int default null, year_to int default null,
                             type_filter text default null, max_rows int default 50, client text default null, industry_filter text default null, country_filter text default null, region_filter text default null, typed text default null)
                           returns table (log_id bigint, rank int, library_id text, company text, wba_id text, year int, doc_type text, label text, url text, page int, score real, excerpt text) language sql as $f$
                             select 1::bigint, g, 'D' || company_filter, 'x', company_filter, 2024, 'Annual Report', 'the report', 'http://x', g, 1::real, 'e' from generate_series(5, 12) g $f$""")
            read = "select public.survey_read('k', 'q?', 'w', null, 'S3', 'Gamma', null, null, 6)"
            cur.execute("select is_on from public.search_options where name = 'company_first'")
            self.assertTrue(cur.fetchone()[0])                                                           # another option is on: it must not switch these on
            cur.execute(read); once = cur.fetchone()[0]
            self.assertEqual((once["status"], once["pages"], once["in"]), ("not_stated", 6, 4000))
            cur.execute("select set_config('lantern.survey_again', 'on', true)")
            cur.execute(read); twice = cur.fetchone()[0]
            self.assertEqual((twice["status"], twice["page"], twice["pages"], twice["in"]), ("yes", 7, 12, 8000))     # pages 5 and 6 were read already: 7 to 12 are the next
            cur.execute("select set_config('lantern.survey_again', 'off', true)")
            cur.execute("select public.survey_instructions(true) like '%this company itself%', public.survey_instructions() like '%this company itself%', public.survey_option('survey_strict')")
            self.assertEqual(cur.fetchone(), (True, False, False))                                       # the strict wording is an option, off
            cur.execute("select public.survey_read('k', 'q?', 'w', null, 'S6', 'Epsilon', null, null, 6)")           # one reading, as the measurement asks it
            one = cur.fetchone()[0]
            self.assertEqual((one["status"], one["library_id"], one["page"], one["printed"], one["pages"], one["in"]), ("yes", "DS6", 1, True, 6, 4000))
            # a row checked against its page: right, wrong, or taken back; only a yes or a no; only by the asker or the owner
            mark = lambda wba, v: (cur.execute("select public.survey_verdict('k', %s, %s, %s)", (made["id"], wba, v)), cur.fetchone()[0])[1]
            self.assertEqual(mark("S1", "right"), {"marked": 1, "verdict": "right", "right": 1, "wrong": 0})
            self.assertEqual(mark("S6", " Wrong ")["wrong"], 1)
            self.assertEqual(mark("S4", "right")["marked"], 0)                                           # "no page found" cannot be checked this way
            cur.execute("select public.survey_get('k', %s)", (made["id"],)); got = cur.fetchone()[0]
            self.assertEqual((got["counts"]["right"], got["counts"]["wrong"], next(r for r in got["rows"] if r["company"] == "Alpha")["verdict"]), (1, 1, "right"))
            self.assertEqual(mark("S1", "")["right"], 0)                                                 # taken back
            cur.execute("select verdict_by from public.survey_rows where survey_id = %s and wba_id = 'S6'", (made["id"],))
            self.assertEqual(cur.fetchone()[0], "ana@worldbenchmarkingalliance.org")
            cur.execute("savepoint bad")
            with self.assertRaises(psycopg.errors.InvalidParameterValue):
                cur.execute("select public.survey_verdict('k', %s, 'S1', 'maybe')", (made["id"],))
            cur.execute("rollback to savepoint bad")
            # a survey that stopped at the ceiling is carried on to the rest of its scope, as part of the same survey
            more = lambda: (cur.execute("select public.survey_more('k', %s)", (made["id"],)), cur.fetchone()[0])[1]
            got = more()                                                                                 # the day's one survey is used, and this is not another
            self.assertEqual((got["added"], got["status"], got["counts"]["companies"], got["counts"]["pending"], got["in_scope"]), (1, "open", 5, 1, 5))
            self.assertEqual([r["company"] for r in got["rows"] if r["status"] == "pending"], ["Gamma"])     # the one the ceiling left out; what was read stays
            self.assertTrue(more()["none"])                                                              # nothing is left to add
            cur.execute("select count(*), sum(companies) from public.survey_extensions where survey_id = %s", (made["id"],))
            self.assertEqual(cur.fetchone(), (1, 1))                                                     # kept, for a price later
            cur.execute(offer); o = cur.fetchone()[0]
            self.assertEqual((o["left_today"], o["per_day"]), (0, 1))                                    # the day's count is the surveys started
            cur.execute("select public.survey_step('k', %s, 5)", (made["id"],)); got = cur.fetchone()[0]
            self.assertEqual((got["status"], got["counts"]["not_stated"]), ("done", 1))
            # a group named in the question by a ranking: the top N of a benchmark is the scope, not what a search finds
            cur.execute("""create table if not exists public.company_scores (score_id text primary key, wba_id text not null, benchmark_id text, benchmark_name text,
                             methodology_id text, year int, transformation text, score numeric, category text, source_updated timestamptz, loaded timestamptz not null default now())""")
            for i, (wba, bench, method, year, score) in enumerate((("S1", "Gender Benchmark", "GEB-2025", 2025, 40), ("S3", "Gender Benchmark", "GEB-2025", 2025, 90), ("S4", "Gender Benchmark", "GEB-2025", 2025, 70),
                                                                  ("S9", "Gender Benchmark", "GEB-2025", 2025, 80), ("S2", "Gender Benchmark", "GEB-2025", 2025, None), ("S3", "Gender Benchmark", "GEB-2023", 2023, 1),
                                                                  ("S6", "Gender Assessment", "GEA-2025", 2025, 99), ("S1", "Nature Benchmark", "NAB-2025", 2025, 99))):
                cur.execute("insert into public.company_scores (score_id, wba_id, benchmark_name, methodology_id, year, score) values (%s, %s, %s, %s, %s, %s)", (f"t{i}", wba, bench, method, year, score))
            cohort = lambda q, within=None: (cur.execute("select public.question_cohort(%s, %s)", (q, within)), cur.fetchone()[0])[1]
            got = cohort("How many of the top 3 performers of the WBA Gender Benchmark have a scope 1 emissions target?")
            self.assertEqual(got, {"label": "the top 3 of the Gender Benchmark 2025", "ids": ["S3", "S9", "S4"], "n": 3})      # by score, the latest year, no score no place
            self.assertEqual(cohort("top 2 of the gender benchmark?", ["S1", "S4"])["ids"], ["S4", "S1"])                     # within a filter
            self.assertIsNone(cohort("Which top 10 companies have a gender policy?"))                                          # no benchmark named
            self.assertIsNone(cohort("Is the gender benchmark mentioned by the companies?"))                                   # no "top N"
            self.assertIsNone(cohort("the top 5 of the climate benchmark"))                                                    # a benchmark the scores do not hold
            # a group named by a grade: a benchmark that publishes letters and no number
            for i, (wba, grade) in enumerate((("S1", "B"), ("S3", "b "), ("S4", "A"), ("S6", "C"), ("S2", None))):
                cur.execute("insert into public.company_scores (score_id, wba_id, benchmark_name, methodology_id, year, category) values (%s, %s, 'ACT Core', 'ACO-2025', 2025, %s)", (f"g{i}", wba, grade))
            got = cohort("How many of the companies that got a B in ACT Core benchmark have executive compensation linked to GHG emissions?")
            self.assertEqual(got, {"label": "the 2 companies with a B in ACT Core 2025", "ids": ["S1", "S3"], "n": 2, "grade": "B"})
            got = cohort("Do the companies rated B or above in ACT Core disclose scope 3?")
            self.assertEqual((got["ids"], got["label"]), (["S4", "S1", "S3"], "the 3 companies with a B or above in ACT Core 2025"))
            self.assertEqual(cohort("Which companies with an A in the ACT Core benchmark have a transition plan?")["ids"], ["S4"])
            self.assertEqual(cohort("C rated companies of ACT Core: do they have a target?", ["S1", "S6"])["ids"], ["S6"])       # within a filter
            self.assertIsNone(cohort("Which ACT Core companies have a target?"))                                               # "a" is the article
            self.assertIsNone(cohort("Which companies got a D in ACT Core?"))                                                  # a letter nobody holds
            self.assertIsNone(cohort("Which companies got a B in the gender benchmark?"))                                      # a benchmark with no grades
            # a ranking by revenue or by number of employees: the robot's figures, the company's latest year, in US dollars
            import datetime
            y = datetime.date.today().year
            for i, (wba, attr, year, value, unit) in enumerate((("S1", "GMC_00048.1", y, 100, "USD"), ("S1", "GMC_00048.1", y, 300, "US$"), ("S1", "GMC_00048.1", y - 1, 900, "USD"),
                                                                ("S3", "GMC_00048.1", y - 1, 20000, "JPY"), ("S4", "GMC_00048.1", y, 1e15, "USD"), ("S6", "GMC_00048.1", y, 200, "XYZ"),
                                                                ("S2", "GMC_00048.1", y - 9, 5000, "USD"), ("S1", "GMC_02526.1", y, 10, "employees"), ("S3", "GMC_02526.1", y, 50, "people"),
                                                                ("S4", "GMC_02526.1", y, 7, "USD"))):
                cur.execute("insert into public.value_versions (version_id, wba_id, attribute, year, value, unit, printed, library_id, page, method) values (%s, %s, %s, %s, %s, %s, 'x', 'L', 1, 'm')",
                            (f"rank{i}", wba, attr, year, value, unit))
            got = cohort("How many of the top 2 companies by revenue in the world disclose scope 1 emissions?")
            self.assertEqual((got["ids"], got["held"], got["metric"]), (["S3", "S1"], 2, "revenue"))        # yen to dollars; the middle of this year's two reads; an impossible figure, an unknown currency and an old year are out
            self.assertEqual(got["label"], "the top 2 by revenue of the 2 companies a figure is held for")
            got = cohort("How many of the largest 10 companies by employee number disclose scope 1 emissions?", ["S1", "S4"])
            self.assertEqual((got["ids"], got["held"]), (["S1"], 1))                                         # within a filter; a figure in dollars is not a number of people
            cur.execute("select public.currency_code(' rmb '), public.currency_code('NT$'), public.currency_code('')")
            self.assertEqual(cur.fetchone(), ("CNY", "TWD", None))
            jwt("e.owner@worldbenchmarkingalliance.org")
            cur.execute("select public.admin_surveys('k', null, null, null, 'ana@worldbenchmarkingalliance.org', 9)")
            jwt("ana@worldbenchmarkingalliance.org")
            asked = "How many of the top 3 performers of the WBA Gender Benchmark have an incentive?"
            cur.execute("select public.survey_offer('k', %s)", (asked,)); o = cur.fetchone()[0]
            self.assertEqual((o["cohort"], o["companies"], o["in_scope"], o["up_to"]), ("the top 3 of the Gender Benchmark 2025", 3, 3, False))
            cur.execute("select public.survey_start('k', %s, null, null, null, null, null, null, 'test')", (asked,)); top = cur.fetchone()[0]
            self.assertEqual((top["companies"], top["cohort"]), (3, "the top 3 of the Gender Benchmark 2025"))
            cur.execute("select public.survey_get('k', %s)", (top["id"],)); got = cur.fetchone()[0]
            self.assertEqual((sorted(r["company"] for r in got["rows"]), got["cohort"]), (["Delta", "Gamma", "S9"], "the top 3 of the Gender Benchmark 2025"))   # a company the library does not know keeps its place
            cur.execute("select public.survey_more('k', %s)", (top["id"],)); self.assertTrue(cur.fetchone()[0]["none"])        # a ranking has no rest
            # 24 Nov 2026: a group picked on the page (its ids joined by ";") is the scope, exactly those companies; one id, or ids the library does not know, are no group
            cur.execute("select string_agg(wba_id, ';' order by wba_id) from public.companies where name in ('Delta', 'Gamma')"); two = cur.fetchone()[0]
            asked = "Do these companies have an incentive tied to emissions?"
            cur.execute("select public.survey_picked(%s, null), public.survey_picked(%s, 'x'), public.survey_picked('NOPE_1;NOPE_2', 'x')", (two.split(";")[0], two + ";NOPE_1"))
            one, known, none = cur.fetchone(); self.assertEqual((one, known["n"], known["label"], none), (None, 2, "x", None))
            cur.execute("select public.survey_offer('k', %s, null, null, null, null, null, %s, '2 companies, testing')", (asked, two)); o = cur.fetchone()[0]
            self.assertEqual((o["cohort"], o["companies"], o["in_scope"], o["up_to"], o["have"]), ("2 companies, testing", 2, 2, False, None))
            cur.execute("select public.survey_start('k', %s, null, null, null, null, null, null, 'test', %s, '2 companies, testing')", (asked, two)); grp = cur.fetchone()[0]
            self.assertEqual((grp["companies"], grp["cohort"]), (2, "2 companies, testing"))
            cur.execute("select public.survey_get('k', %s)", (grp["id"],)); self.assertEqual(sorted(r["company"] for r in cur.fetchone()[0]["rows"]), ["Delta", "Gamma"])
            cur.execute("select public.survey_offer('k', %s, null, null, null, null, null, %s, 'another name')", (asked, two)); self.assertEqual(cur.fetchone()[0]["have"]["id"], grp["id"])   # the same group again: the survey already run
            cur.execute("select public.survey_offer('k', %s)", (asked,)); self.assertIsNone(cur.fetchone()[0]["have"])           # the same question with no group is another survey
            cur.execute("select public.survey_more('k', %s)", (grp["id"],)); self.assertTrue(cur.fetchone()[0]["none"])        # a picked group has no rest
            # the model refuses the reader (no credit): the survey stops, and no company is marked "not read"
            cur.execute("""create or replace function public.ask_model(api_key text, http_schema text, model text, system_text text, user_text text, max_tokens int) returns jsonb language sql as $f$
                             select jsonb_build_object('error', 'the model answered HTTP 400: Your credit balance is too low to access the API.') $f$""")
            cur.execute("select public.survey_step('k', %s, 5)", (top["id"],)); got = cur.fetchone()[0]
            self.assertTrue(got["off"]); self.assertIn("out of credit", got["why"])
            self.assertEqual((got["counts"]["failed"], got["status"]), (0, "open"))
            self.assertGreaterEqual(got["counts"]["pending"], 1)
            jwt("someone.else@worldbenchmarkingalliance.org")
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):                              # a survey is read on by its asker alone
                cur.execute("select public.survey_step('k', %s, 1)", (made["id"],))
            c.rollback()

    def test_what_a_reading_said(self):
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            parse = lambda text: (cur.execute("select public.survey_parse(%s)", (text,)), cur.fetchone()[0])[1]
            self.assertEqual(parse('I think. {"answer": "YES", "value": "x", "evidence": "It says so.", "year": 2024, "source": 3} done'),
                             {"status": "yes", "value": "x", "evidence": "It says so.", "year": 2024, "source": 3})
            self.assertEqual(parse('{"answer": "not stated", "value": null, "evidence": null, "year": "soon", "source": "the first"}')["status"], "not_stated")
            self.assertEqual(parse('{"answer": "no", "source": "[3]"}')["source"], 3)                    # 3, "3" and "[3]" are the same page
            self.assertIsNone(parse('{"answer": "no", "source": "pages 3 and 4"}')["source"])
            self.assertIsNone(parse('{"answer": "maybe"}')); self.assertIsNone(parse("no json here")); self.assertIsNone(parse('{"answer": "yes"')); self.assertIsNone(parse(None))
            cur.execute("select public.survey_instructions()")
            text = cur.fetchone()[0]
            self.assertIn('A page that does not mention the matter is not a "no"', text)
            self.assertIn("Never use what you know from elsewhere", text)
            c.rollback()

    def test_the_owner_sets_how_many_answers_a_day_a_person_has(self):
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("select public.ask_per_day('new@x.org')")
            self.assertEqual(cur.fetchone()[0], 50)                              # what everyone has
            cur.execute("select public.admin_ask_limit_set('k', ' Luis@X.org ', 120), public.admin_ask_limit_set('k', 'not an address', 10), public.admin_ask_limit_set('k', 'a@x.org', 501)")
            ok, bad, high = cur.fetchone()
            self.assertEqual((ok["email"], ok["per_day"], "error" in bad, "error" in high), ("luis@x.org", 120, True, True))
            cur.execute("select public.admin_ask_limit_set('k', 'luis@x.org', 80)")          # set again: one line, the new number
            cur.execute("select public.ask_per_day('LUIS@x.org'), (select count(*) from public.ask_limits where email = 'luis@x.org')")
            self.assertEqual(cur.fetchone(), (80, 1))
            cur.execute("insert into public.ask_log (asker, question, answered) values ('luis@x.org', 'q one?', true), ('luis@x.org', 'q two?', false)")
            cur.execute("select public.admin_overview('k')")
            person = [p for p in cur.fetchone()[0]["people"] if p["email"] == "luis@x.org"][0]
            self.assertEqual((person["per_day"], person["answers_today"], person["answers"]), (80, 1, 1))
            cur.execute("select public.admin_ask_limit_set('k', 'soon@x.org', 5)")
            cur.execute("select public.admin_overview('k')")
            self.assertIn("soon@x.org", [p["email"] for p in cur.fetchone()[0]["people"]])     # listed before a first question
            cur.execute("select has_function_privilege('authenticated', 'public.ask_per_day(text)', 'execute')")
            self.assertFalse(cur.fetchone()[0])
            c.rollback()

    def test_the_owner_sees_what_a_link_searched_and_a_person_asked(self):
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("insert into public.search_log (key_name, q, typed, company, year_from, n, ms, at) values "
                        "('luis', 'scope 1', 'nike scope 1 2023', 'Nike', 2023, 24, 310, now() - interval '2 hours'), "
                        "('luis', 'water', null, null, null, 8, 90, now() - interval '1 hour'), ('team', 'other', 'other', null, null, 1, 5, now()) returning id")
            first = cur.fetchone()[0]
            cur.execute("insert into public.search_clicks (log_id, library_id, page, rank) values (%s, 'L', 3, 1)", (first,))
            cur.execute("insert into public.ask_log (asker, question, answered, error) values ('p@x.org', 'What is Nike scope 1?', true, null), ('p@x.org', 'Broken?', false, 'x'), ('q@x.org', 'Not mine', true, null)")
            cur.execute("select public.admin_searches('k', 'luis')"); got = cur.fetchone()[0]
            self.assertEqual([(x["typed"], x["as_typed"], x["clicks"]) for x in got["searches"]], [("water", False, 0), ("nike scope 1 2023", True, 1)])   # newest first; older rows fall back to the words read
            self.assertEqual((got["searches"][1]["company"], got["searches"][1]["year_from"], got["questions"]), ("Nike", 2023, []))
            # what the reader finally saw: the page reports it after the list is shown
            self.assertEqual((got["searches"][1]["shown"], got["searches"][1]["meaning"]), (None, None))
            cur.execute("select public.search_key_name('k')"); kn = cur.fetchone()[0]
            cur.execute("insert into public.search_log (key_name, q, typed, n, ms) values (%s, 'rebaseline', 'rebaseline polyus', 0, 40), ('team', 'rebaseline', 'rebaseline polyus', 0, 40) returning id", (kn,))
            mine, other = [r[0] for r in cur.fetchall()]
            seen = lambda i: (cur.execute("select shown, by_meaning, meaning from public.search_log where id = %s", (i,)), cur.fetchone())[1]
            cur.execute("select public.search_seen('k', null, 'rebaseline polyus', 7, 7, 'used')")          # no page by words, so no log id: the key's latest search of that text
            self.assertEqual((seen(mine), seen(other)), ((7, 7, "used"), (None, None, None)))
            cur.execute("select public.search_seen('k', %s, 'x', 3, 0, 'late')", (mine,)); self.assertEqual(seen(mine), (3, 0, "late"))
            cur.execute("select public.search_seen('k', %s, 'x', 9, 9, 'anything')", (mine,)); self.assertEqual(seen(mine), (3, 0, "late"))   # not a state
            cur.execute("select public.search_seen('k', %s, 'x', 9, 9, 'used')", (other,)); self.assertEqual(seen(other), (None, None, None))   # another key's search
            cur.execute("select public.admin_searches('k', %s)", (kn,)); self.assertEqual([(x["shown"], x["by_meaning"], x["meaning"]) for x in cur.fetchone()[0]["searches"]][:1], [(3, 0, "late")])
            # the searches that went badly: the same company again and again with nothing opened, nothing shown, few pages, slow
            cur.execute("insert into public.search_log (key_name, q, typed, company, n, shown, by_meaning, meaning, ms, at) values "
                        "('hard', 'a', 'baseline acme', 'PT_1', 39, 39, 0, 'used', 60, now() - interval '9 minutes'), "
                        "('hard', 'b', 'base year acme', 'PT_1', 0, 0, 0, 'none', 40, now() - interval '8 minutes'), "
                        "('hard', 'c', 'base year target acme', 'PT_1', 4, 2, 1, 'used', 6200, now() - interval '7 minutes'), "
                        "('hard', 'd', 'rebaseline acme', 'PT_1', 0, 5, 5, 'used', 50, now() - interval '6 minutes'), "
                        "('hard', 'e', 'other company', 'PT_2', 20, 20, 0, 'used', 50, now() - interval '5 minutes'), "
                        "('hard', 'f', 'old search', null, 0, null, null, null, 50, now() - interval '30 days')")
            # a comparison's own searches (client marked trial) are not a person's: never in the review
            cur.execute("insert into public.search_log (key_name, q, typed, company, n, shown, ms, client, at) select 'hard', 'z', 'a trial question', 'PT_9', 0, 0, 40, 'ask trial', now() - make_interval(secs => g) from generate_series(1, 4) g")
            cur.execute("select public.admin_hard_searches('k', 7)"); hard = cur.fetchone()[0]
            self.assertEqual([(x["key_name"], x["company"], x["searches"], x["typed"][0], x["typed"][-1]) for x in hard["runs"] if x["key_name"] == "hard"], [("hard", "PT_1", 4, "baseline acme", "rebaseline acme")])
            self.assertIn("base year acme", [x["typed"] for x in hard["nothing"]])
            self.assertNotIn("a trial question", [x["typed"] for x in hard["nothing"]])
            self.assertNotIn("rebaseline acme", [x["typed"] for x in hard["nothing"]])              # no page by words, five by meaning: something was shown
            self.assertNotIn("old search", [x["typed"] for x in hard["nothing"]])                   # outside the days asked
            self.assertEqual([x["typed"] for x in hard["few"] if x["key_name"] == "hard"], ["base year target acme"])
            cur.execute("insert into public.search_clicks (log_id, library_id, page, rank) select id, 'L', 1, 1 from public.search_log where key_name = 'hard' and typed in ('baseline acme', 'base year target acme')")
            cur.execute("select public.admin_hard_searches('k', 7)"); again = cur.fetchone()[0]              # a result opened changes nothing: the passage is read in the list
            self.assertEqual([(x["searches"], x["opened"]) for x in again["runs"] if x["key_name"] == "hard"], [(4, 2)])
            self.assertEqual([x["typed"] for x in again["few"] if x["key_name"] == "hard"], ["base year target acme"])
            self.assertEqual([(x["typed"], x["ms"]) for x in hard["slow"] if x["key_name"] == "hard"], [("base year target acme", 6200)])
            cur.execute("select has_function_privilege('anon', 'public.admin_hard_searches(text, int)', 'execute')"); self.assertFalse(cur.fetchone()[0])
            cur.execute("select public.admin_searches('k', null, 'p@x.org')"); got = cur.fetchone()[0]
            self.assertEqual(got["searches"], [])
            self.assertEqual(sorted((x["question"], x["answered"], x["failed"]) for x in got["questions"]), [("Broken?", False, True), ("What is Nike scope 1?", True, False)])
            cur.execute("select has_function_privilege('anon', 'public.admin_searches(text, text, text, int)', 'execute')")
            self.assertFalse(cur.fetchone()[0])                                 # the owner only: never the open link
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

    def test_an_answer_about_a_company_may_take_more_pages_of_one_document(self):
        """Option ask_doc_pages (16 Nov 2026): three pages a document is right for a list, not for an answer about one company."""
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("select public.pages_a_document()"); plain = cur.fetchone()[0]
            got = []
            for v in ("6", "9", "0", "12", "x", ""):
                cur.execute("select set_config('lantern.per_doc', %s, true)", (v,))
                cur.execute("select public.pages_a_document()"); got.append(cur.fetchone()[0])
            # the search as ask_pages sees it: a stub that says, in the excerpt, how many pages a document it was allowed
            cur.execute("""create or replace function public.find_blend(key text, q text, qvec real[] default null, company_filter text default null, year_from int default null, year_to int default null,
                             type_filter text default null, max_rows int default 50, client text default null, industry_filter text default null, country_filter text default null,
                             region_filter text default null, typed text default null)
                           returns table (log_id bigint, rank int, library_id text, company text, wba_id text, year int, doc_type text, label text, url text, page int, score real, excerpt text)
                           language sql as $f$
                             select null::bigint, g, 'D', 'C', 'W', 2024, 't', 'l', null::text, g, 0::real, public.pages_a_document()::text
                             from generate_series(1, least(max_rows, 24)) g $f$""")
            ask = "select distinct excerpt from public.ask_pages('k', 'q', null, %s, 2024, 2024, null, 'q?')"
            after = "select coalesce(current_setting('lantern.per_doc', true), '')"
            cur.execute(ask, ("C",)); default = cur.fetchall()
            cur.execute("select set_config('lantern.ask_doc_pages', 'on', true)")
            cur.execute(ask, ("C",)); on = cur.fetchall()
            cur.execute(after); left = cur.fetchone()[0]
            cur.execute(ask, (None,)); no_company = cur.fetchall()
            cur.execute("select set_config('lantern.ask_doc_pages', 'off', true)")
            cur.execute(ask, ("C",)); off = cur.fetchall()
            cur.execute("select is_on from public.search_options where name = 'ask_doc_pages'"); stored = cur.fetchone()[0]
            c.rollback()
        self.assertEqual(plain, 3)
        self.assertEqual(got, [6, 9, 3, 3, 3, 3])                                                    # a digit from 1 to 9, or three
        self.assertEqual((default, off, no_company), ([("3",)], [("3",)], [("3",)]))                 # off until it is compared; no company: a list
        self.assertEqual(on, [("6",)])
        self.assertEqual(left, "")                                                                  # put back: the next search of the transaction is a list again
        self.assertFalse(stored)

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
        later = [(g, 2023, "ask") for g in range(1, 6)] + [(200 + g, 2024, "ask later") for g in range(1, 4)]      # the best five of the year asked, then three pages of later reports
        self.assertEqual(off, eight); self.assertEqual(default, later)                               # on since 18 Nov 2026 (compared on 15 Nov); the session can still set it off
        self.assertEqual(on, later)
        self.assertEqual([p for p, _, _ in span], [1, 2, 3, 4, 5, 201, 202, 203])                   # a span of years: later than its last year
        self.assertEqual(newest, [(g, 2025, "ask") for g in range(1, 9)])                            # no main report after 2025 (two pages, or statutory, do not count)
        self.assertEqual(no_company, eight)                                                         # no company named: whose later reports?

    def test_a_question_about_a_kind_of_content_reads_pages_found_by_the_reports_words(self):
        """23 Nov 2026: "main difficulties <company> in 2025" was answered from notes to the accounts. With option
        ask_report_words the answer reads up to three pages found by the words reports use (risks, challenges,
        outlook) after the search's best five; off by default; never without a company or a concept in the question."""
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            says = lambda t: (cur.execute("select public.ask_concept_says(%s)", (t,)), cur.fetchone()[0])[1]
            self.assertIn("headwinds", says("Main DIFFICULTIES, in 2025?")); self.assertIn("risk factors", says("key risks")); self.assertIn("priorities", says("why this strategy"))
            self.assertIsNone(says("scope 1 emissions")); self.assertIsNone(says("risky")); self.assertIsNone(says(None))
            cur.execute("""create or replace function public.find_blend(key text, q text, qvec real[] default null, company_filter text default null, year_from int default null, year_to int default null,
                             type_filter text default null, max_rows int default 50, client text default null, industry_filter text default null, country_filter text default null,
                             region_filter text default null, typed text default null)
                           returns table (log_id bigint, rank int, library_id text, company text, wba_id text, year int, doc_type text, label text, url text, page int, score real, excerpt text)
                           language sql as $f$ select null::bigint, g, 'DOC', 'C', 'W', 2025, 'Annual Report', 'l', null::text, g, 0::real, client from generate_series(1, least(max_rows, 24)) g $f$""")
            # the pages inside the company's main reports that hold most of the reports' words: page 3 is one of the best five already
            cur.execute("""create or replace function public.ask_concept_pages(company_filter text, year_from int, year_to int, says text, max_rows int default 3)
                           returns table (library_id text, company text, wba_id text, year int, doc_type text, label text, url text, page int, excerpt text, held int)
                           language sql as $f$ select 'DOC', 'C', 'W', 2025, 'Annual Report', 'l', null::text, (array[3, 7, 91, 92, 90])[g], says, 9 - g from generate_series(1, 5) g $f$""")
            ask = "select page, excerpt from public.ask_pages('k', %s, null, %s, 2025, 2025, null, %s)"
            cur.execute("select set_config('lantern.ask_later', 'off', true)")
            cur.execute("select set_config('lantern.ask_long_answers', 'off', true)")           # on in the table since 24 Nov 2026: this part is about ask_report_words alone
            cur.execute(ask, ("main difficulties", "C", "main difficulties in 2025")); default = cur.fetchall()
            cur.execute("select set_config('lantern.ask_report_words', 'on', true)")
            cur.execute(ask, ("main difficulties", "C", "main difficulties in 2025")); on = cur.fetchall()
            cur.execute(ask, ("scope 1", "C", "scope 1 in 2025")); no_concept = cur.fetchall()
            cur.execute(ask, ("main difficulties", None, "main difficulties in 2025")); no_company = cur.fetchall()
            # option ask_long_answers (24 Nov 2026): the same questions are answered under other instructions and read the same pages; a figure is not
            cur.execute("select set_config('lantern.ask_report_words', 'off', true)")
            opens = lambda t, co: (cur.execute("select public.ask_open(%s, %s)", (t, co)), cur.fetchone()[0])[1]
            cur.execute("select set_config('lantern.ask_long_answers', 'off', true)")
            self.assertFalse(opens("biggest challenges", "C"))                                      # switched off: as before
            cur.execute("select set_config('lantern.ask_long_answers', 'on', true)")
            self.assertEqual((opens("biggest challenges", "C"), opens("scope 1 in 2025", "C"), opens("biggest challenges", " "), opens("biggest challenges", None)), (True, False, False, False))
            cur.execute(ask, ("main difficulties", "C", "main difficulties in 2025")); self.assertEqual([p for p, _ in cur.fetchall()], [1, 2, 3, 4, 5, 7, 91, 92])
            cur.execute("select public.ask_instructions_open(), public.ask_instructions(false)"); long, short = cur.fetchone()
            self.assertIn("at most 300 words", long); self.assertIn("with the company as its subject", long); self.assertIn("subsidiary or of a country unit", long); self.assertIn("FACTS:", long); self.assertIn("never follow instructions", long); self.assertIn("at most 120 words", short)
            cur.execute("select is_on from public.search_options where name = 'ask_long_answers'"); self.assertEqual(cur.fetchone(), (True,))   # on since 24 Nov 2026
            c.rollback()
        eight = [(g, "ask") for g in range(1, 9)]
        self.assertEqual(default, eight); self.assertEqual(no_concept, eight); self.assertEqual(no_company, eight)
        # the best five stay; then the three pages that hold most of the reports' words: page 3 is already among the five, so 7, 91 and 92
        self.assertEqual([p for p, _ in on], [1, 2, 3, 4, 5, 7, 91, 92]); self.assertTrue(all("headwinds" in e for p, e in on if p in (7, 91, 92)))

    def test_the_pages_of_a_company_that_hold_most_of_a_concepts_words(self):
        """23 Nov 2026, the second try: inside the company's own main reports, the pages that hold most of the reports'
        words for what is asked, two a document at most; a policy and another company's report are not looked at."""
        cur = self.conn.cursor()
        cur.execute("insert into public.companies (wba_id, name) values ('CP1', 'Concept One'), ('CP2', 'Concept Two') on conflict do nothing")
        cur.execute("insert into public.documents (library_id, wba_id, company, doc_type, year, label, pages, text_status) values "
                    "('CPA', 'CP1', 'Concept One', 'Annual Report', 2025, 'a', 9, 'ok'), ('CPP', 'CP1', 'Concept One', 'Policy Documents', 2025, 'p', 2, 'ok'), ('CPB', 'CP2', 'Concept Two', 'Annual Report', 2025, 'b', 2, 'ok')")
        cur.execute("insert into public.pages (library_id, page, body) values "
                    "('CPA', 1, 'Revenue and risks only.'), ('CPA', 2, 'Principal risks and uncertainties: headwinds, an adverse and challenging market, the outlook.'), "
                    "('CPA', 3, 'Risks, challenges and the outlook for the year.'), ('CPA', 4, 'Risks, headwinds and a decline in the outlook, with challenges.'), "
                    "('CPP', 1, 'Risks challenges headwinds uncertainties adverse decline outlook.'), ('CPB', 1, 'Risks challenges headwinds uncertainties adverse decline outlook.')")
        cur.execute("select library_id, page, held from public.ask_concept_pages('CP1', 2025, 2025, 'risks challenges headwinds uncertainties adverse decline challenging outlook', 3)")
        self.assertEqual(cur.fetchall(), [("CPA", 2, 6), ("CPA", 4, 5)])            # the two best of the report; page 1 holds one word, page 3 would be a third of one document
        cur.execute("select count(*) from public.ask_concept_pages('CP1', 2025, 2025, null, 3)"); self.assertEqual(cur.fetchone()[0], 0)
        cur.execute("select count(*) from public.ask_concept_pages('CP1', null, null, 'risks challenges outlook', 3)"); self.assertEqual(cur.fetchone()[0], 2)   # no year asked: the newest reports; pages 3 and 4 hold all three
        cur.execute("delete from public.pages where library_id in ('CPA', 'CPP', 'CPB')"); cur.execute("delete from public.documents where library_id in ('CPA', 'CPP', 'CPB')"); cur.execute("delete from public.companies where wba_id in ('CP1', 'CP2')")

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

    def test_one_companys_main_reports_of_the_last_three_years(self):
        import datetime, psycopg
        last = datetime.date.today().year - 1
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("create table if not exists public.companies (wba_id text primary key, name text, industry text, country text, region text, website text, lantern_id text)")
            cur.execute("insert into public.companies (wba_id, name) values ('CG_ONE', 'One Co') on conflict do nothing")
            for year, kind in [(last, "Sustainability Report"), (last - 1, "Annual Report"), (last - 1, "ESG Report"), (last - 2, "Policy Documents")]:
                cur.execute("insert into public.documents (library_id, wba_id, company, doc_type, year) values (%s, 'CG_ONE', 'One Co', %s, %s)", (f"CG-{year}-{kind}", kind, year))
            cur.execute("insert into public.gap_notes (wba_id, kind, year, reason) values ('CG_ONE', 'annual', %s, 'not_published')", (last - 2,))
            cur.execute("select public.company_gaps('k', 'CG_ONE'), public.company_gaps('k', 'CG_ONE;OTHER'), public.company_gaps('k', 'NOBODY'), public.company_gaps('k', null)")
            got, two, nobody, none = cur.fetchone()
            self.assertEqual((got["from"], got["to"]), (last - 2, last))
            self.assertEqual([(y["year"], y["annual"], y["sustainability"], y["annual_note"]) for y in got["years"]],
                             [(last, False, True, None), (last - 1, True, True, None), (last - 2, False, False, "not_published")])   # newest first; a person's note travels with the year
            self.assertEqual((two, nobody, none), (None, None, None))                                # one company the library follows, or nothing
            c.rollback()

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

    def test_the_reference_list_answers_a_name_an_acronym_and_a_question(self):
        """22 Nov 2026: a search that is the name or the acronym of an entry of the reference list gets its card; the
        loader upserts by id and marks gone what left the file. Invented entries: the list itself is private."""
        import psycopg, load_reference as R
        entries = [{"id": "norm:t1", "kind": "norm", "name": "Invented Reporting Rule (IRR)", "aliases": ["IRR"], "year": 2019, "url": "https://rule.example/irr", "text_en": "A rule invented for a test.", "issuers": ["Invented Board"]},
                   {"id": "term:t2", "kind": "term", "name": "Made-up Materiality", "text_en": "A term invented for a test.", "text_es": "Un término inventado."},
                   {"id": "organisation:t3", "kind": "organisation", "name": "IRR", "text_en": "An organisation that shares the acronym."},
                   {"id": "term:t4", "kind": "term", "name": "Imaginary Targets", "text_en": "Plural name."}]
        with psycopg.connect(URL) as c:
            rows, skipped = R.entry_rows(entries); p = R.plan(c, rows); R.write(c)
            self.assertEqual((skipped, p["new"], p["changed"], p["gone"]), (0, 4, 0, 0))
            p2 = R.plan(c, rows); c.rollback(); self.assertEqual((p2["new"], p2["changed"], p2["same"]), (0, 0, 4))      # run again: nothing to do
        cur = self.conn.cursor()
        look = lambda q: (cur.execute("select public.reference_lookup('', %s)", (q,)), cur.fetchone()[0])[1]
        self.assertEqual([e["name"] for e in look("made-up materiality")], ["Made-up Materiality"])
        self.assertEqual([e["name"] for e in look("What is Made-Up Materiality?")], ["Made-up Materiality"])
        self.assertEqual([e["name"] for e in look("¿Qué es la made up materiality?")], ["Made-up Materiality"])
        self.assertEqual([e["name"] for e in look("what does IRR stand for")], ["Invented Reporting Rule (IRR)", "IRR"])        # the norm first, then the organisation
        # one thing listed under two kinds is shown once, the norm; a term whose name begins with the words asked answers them
        with psycopg.connect(URL) as c:
            more = entries + [{"id": "term:t5", "kind": "term", "name": "Invented Reporting Rule", "text_en": "The same rule, listed as a term."},
                              {"id": "term:t6", "kind": "term", "name": "Pretend Scope Emissions", "text_en": "Begins with the words asked."},
                              {"id": "norm:t7", "kind": "norm", "name": "Pretend Scope Standard of Somewhere", "text_en": "A norm that begins alike."}]
            rows, _ = R.entry_rows(more); R.plan(c, rows); R.write(c)
        self.assertEqual([(e["kind"], e["name"]) for e in look("invented reporting rule")], [("norm", "Invented Reporting Rule (IRR)")])
        self.assertEqual([e["name"] for e in look("pretend scope")], ["Pretend Scope Emissions"])
        # asked and not in the list: counted for the owner, as compared, questions marked
        cur.execute("delete from public.reference_misses")
        cur.execute("insert into public.search_vocab (word, n) values ('fanciful', 9), ('stewardship', 9) on conflict do nothing")           # words the library knows
        self.assertEqual(look("What is Fanciful Stewardship?"), []); self.assertEqual(look("fanciful  stewardship"), []); self.assertEqual(look("one two three four five six"), [])
        self.assertEqual(look("zyntharix stewardship"), [])                                                                                 # a name no page holds: not a term to add
        cur.execute("select asked_key, asked, question, times from public.reference_misses"); self.assertEqual(cur.fetchall(), [("fanciful stewardship", "fanciful stewardship", True, 2)])
        cur.execute("select public.admin_reference_asked_set('k', 'fanciful stewardship', true)"); self.assertTrue(cur.fetchone()[0]["ok"])
        cur.execute("select public.admin_reference('k')"); adm = cur.fetchone()[0]
        self.assertEqual([(m["asked"], m["times"], m["dismissed"]) for m in adm["asked"]], [("fanciful stewardship", 2, True)]); self.assertGreaterEqual(adm["kinds"]["term"], 3)
        # 23 Nov 2026: the database is the master copy. The owner adds an entry and corrects one; the loader leaves both as they are
        cur.execute("select public.admin_reference_save('k', null, 'term', ' Fanciful  Stewardship ', array['FS', ' '], 'Looking after things that do not exist.', null, 'https://fs.example')"); made = cur.fetchone()[0]
        self.assertTrue(made["ok"]); self.assertEqual([e["name"] for e in look("what is fanciful stewardship")], ["Fanciful Stewardship"]); self.assertEqual([e["name"] for e in look("FS")], ["Fanciful Stewardship"])
        self.assertFalse(look("fs")[0]["ai"])
        cur.execute("select dismissed from public.reference_misses where asked_key = 'fanciful stewardship'"); self.assertEqual(cur.fetchone(), (True,))      # the candidate is answered now
        cur.execute("select public.admin_reference_find('k', 'made-up')"); hit = cur.fetchone()[0]; self.assertEqual([(e["id"], e["origin"]) for e in hit], [("term:t2", "list")])
        cur.execute("select public.admin_reference_save('k', 'term:t2', 'term', 'Made-up Materiality', array['MUM'], 'Corrected by a person.', null, null)")
        for bad in ("select public.admin_reference_save('k', null, 'term', 'Fanciful Stewardship')", "select public.admin_reference_save('k', null, 'thing', 'X')", "select public.admin_reference_save('k', null, 'term', '  ')",
                    "select public.admin_reference_save('k', 'about:lantrna', 'term', 'X')", "select public.admin_reference_save('k', null, 'term', 'Y', null, null, null, 'ftp://x')"):
            with self.assertRaises(psycopg.Error): cur.execute(bad)
        with psycopg.connect(URL) as c:
            rows, _ = R.entry_rows(entries); p = R.plan(c, rows); R.write(c); self.assertEqual((p["changed"], p["gone"]), (0, 3))       # t5..t7 left the file; the corrected and the added entries are not touched
        self.assertEqual(look("made up materiality")[0]["text"], "Corrected by a person."); self.assertEqual(len(look("fanciful stewardship")), 1)
        cur.execute("select public.admin_reference_save('k', %s, 'term', 'Fanciful Stewardship', null, null, null, null, null, true)", (made["id"],)); self.assertEqual(look("fanciful stewardship"), [])   # hidden, not deleted
        cur.execute("select count(*) from public.reference_entries where id = %s and gone", (made["id"],)); self.assertEqual(cur.fetchone()[0], 1)
        cur.execute("delete from public.reference_entries where id = %s", (made["id"],)); cur.execute("update public.reference_entries set edited_at = null, edited_by = null where id = 'term:t2'")
        cur.execute("delete from public.reference_misses"); cur.execute("delete from public.search_vocab where word in ('fanciful', 'stewardship')")
        with psycopg.connect(URL) as c:
            rows, _ = R.entry_rows(entries); R.plan(c, rows); R.write(c); c.execute("delete from public.reference_entries where id in ('term:t5', 'term:t6', 'norm:t7')")
        self.assertEqual([e["name"] for e in look("Invented Reporting Rule")], ["Invented Reporting Rule (IRR)"])               # the name without its bracket
        self.assertEqual([e["name"] for e in look("imaginary target")], ["Imaginary Targets"])                                # singular asked of a plural entry
        self.assertEqual(look("made-up materiality of Acme in 2023"), []); self.assertEqual(look(""), []); self.assertEqual(look(None), [])
        # a text that names lantrNa is about lantrNa: the entry whose own words it uses, else the general one; the loader leaves these alone
        self.assertEqual([e["id"] for e in look("What is lantrNa?")], ["about:lantrna"]); self.assertEqual([e["id"] for e in look("lantrna")], ["about:lantrna"])
        self.assertEqual([e["id"] for e in look("how does lantrNa work?")], ["about:how"]); self.assertEqual([e["id"] for e in look("can I trust lantrna answers")], ["about:answers"])
        self.assertEqual([e["id"] for e in look("how do I add a company to lantrna")], ["about:add"]); self.assertEqual([e["id"] for e in look("¿Qué es lantrNa?")], ["about:lantrna"])
        self.assertFalse(look("lantrna")[0]["ai"]); self.assertEqual(look("irr lantern"), [])
        cur.execute("select count(*) from public.reference_entries where kind = 'about' and not gone"); self.assertEqual(cur.fetchone()[0], 4)
        got = look("irr")[0]; self.assertEqual((got["kind"], got["year"], got["url"], got["issuers"], got["ai"]), ("norm", 2019, "https://rule.example/irr", ["Invented Board"], True))
        # an entry that left the file is kept and no longer shown; one that comes back is shown again
        with psycopg.connect(URL) as c:
            rows, _ = R.entry_rows(entries[:3]); p = R.plan(c, rows); R.write(c); self.assertEqual((p["gone"], p["same"]), (1, 3))
        self.assertEqual(look("imaginary targets"), [])
        cur.execute("select count(*), count(*) filter (where gone) from public.reference_entries where id like '%:t_'"); self.assertEqual(cur.fetchone(), (4, 1))
        with psycopg.connect(URL) as c:
            rows, _ = R.entry_rows(entries); p = R.plan(c, rows); R.write(c); self.assertEqual(p["changed"], 1)
        self.assertEqual(len(look("imaginary targets")), 1)
        with psycopg.connect(URL) as c:
            c.execute("delete from public.reference_entries where id like '%:t_'")

    def test_option_reader_asked_form_changes_two_sentences_and_only_when_on(self):
        """22 Nov 2026: Yes or No only to a question; a part of an overview named only when the pages do not cover it."""
        import psycopg
        with psycopg.connect(URL) as c:
            cur = c.cursor()
            cur.execute("select is_on from public.search_options where name = 'reader_asked_form'"); self.assertEqual(cur.fetchone(), (True,))       # on after its comparison (run 45073687451)
            cur.execute("select set_config('lantern.reader_asked_form', 'off', true)")
            topic = '{"label": "the business", "groups": [{"label": "Identity"}, {"label": "Structure"}]}'
            cur.execute("select public.ask_instructions(false), public.ask_overview_note(%s::jsonb)", (topic,)); plain, note = cur.fetchone()
            self.assertNotIn("not always a question", plain); self.assertIn("Then say in one line which of these parts the excerpts do not cover.", note)
            self.assertIn("an overview of the business for this company", note); self.assertIn("Identity, Structure", note)
            cur.execute("select set_config('lantern.reader_asked_form', 'on', true)")
            cur.execute("select public.ask_instructions(false), public.ask_overview_note(%s::jsonb)", (topic,)); on, note_on = cur.fetchone()
            self.assertTrue(on.startswith(plain)); self.assertIn("Begin with Yes or No only when they are a question", on)
            self.assertIn("when they cover all of them, say nothing about the parts", note_on); self.assertNotIn("Then say in one line", note_on)
            cur.execute("select public.ask_option_trial(1, 'card_scores'), public.ask_option_trial(1, null)")           # not an option of the reader: nothing is called
            self.assertEqual([x["skipped"] for x in cur.fetchone()], ["not an option of the reader"] * 2)
            c.rollback()

    def test_did_you_mean(self):
        import psycopg, load_supabase as L
        with psycopg.connect(URL) as c:
            L.refresh_derived(c)
        cur = self.conn.cursor()
        cur.execute("select public.did_you_mean('', 'unilver scope 3')"); self.assertEqual(cur.fetchone()[0], "unilever scope 3")
        cur.execute("select public.did_you_mean('', 'living wage')"); self.assertIsNone(cur.fetchone()[0])
        # the page's question before it takes a word for a company's name typed wrong
        unknown = lambda w: (cur.execute("select public.word_unknown('', %s)", (w,)), cur.fetchone()[0])[1]
        self.assertTrue(unknown("unilver")); self.assertTrue(unknown(" Severestal "))
        self.assertFalse(unknown("unilever"))                                     # a word of a title or a company's name
        self.assertFalse(unknown("x1")); self.assertFalse(unknown(None))          # not a word
        # a name the library does not hold (22 Nov 2026): not a word of the vocabulary, not a word typed wrong; counted for the owner
        cur.execute("delete from public.company_misses")
        held = lambda ws: (cur.execute("select public.names_not_held('', %s::text[])", (ws,)), cur.fetchone()[0])[1]
        self.assertEqual(held(["Zyntharix", "scope", "unilver", "x1", "zyntharix"]), ["zyntharix"])     # "unilver" is Unilever typed wrong; "scope" is a word
        self.assertEqual(held(["zyntharix"]), ["zyntharix"]); self.assertEqual(held(None), [])
        cur.execute("select word, times, cardinality(people), dismissed from public.company_misses"); self.assertEqual(cur.fetchall(), [("zyntharix", 2, 1, False)])
        cur.execute("select public.admin_name_not_held_set('k', 'Zyntharix', true)"); done = cur.fetchone()[0]
        cur.execute("select public.admin_names_not_held('k')"); rows = cur.fetchone()[0]
        self.assertTrue(done["ok"]); self.assertEqual([(r["word"], r["times"], r["people"], r["dismissed"]) for r in rows], [("zyntharix", 2, 1, True)])
        cur.execute("delete from public.company_misses")

    def test_the_use_of_a_norm_is_counted_by_year_from_the_pages(self):
        """23 Nov 2026: which standards the reports of a year name. Counted from the pages' words by the entry's name
        or acronym as a phrase; an everyday word is not searched as an acronym; the owner sees the year's ranking."""
        import reference_use as U
        cur = self.conn.cursor()
        cur.execute("insert into public.documents (library_id, wba_id, company, doc_type, year, label, pages, text_status) values "
                    "('RU1', 'RUA', 'A', 'Annual Report', 2025, 'a', 2, 'ok'), ('RU2', 'RUA', 'A', 'Sustainability Report', 2025, 'b', 1, 'ok'), "
                    "('RU3', 'RUB', 'B', 'Annual Report', 2025, 'c', 1, 'ok'), ('RU4', 'RUB', 'B', 'Annual Report', 2024, 'd', 1, 'ok')")
        cur.execute("insert into public.pages (library_id, page, body) values ('RU1', 1, 'Prepared under the Invented Zebra Standard.'), ('RU1', 2, 'The IZS applies. We act.'), "
                    "('RU2', 1, 'In line with IZS.'), ('RU3', 1, 'A zebra, and a standard, but not the phrase.'), ('RU4', 1, 'invented zebra standard, in small letters')")
        cur.execute("insert into public.reference_entries (id, kind, name, name_key, aliases) values ('norm:izs', 'norm', 'Invented Zebra Standard', 'invented zebra standard', array['IZS', 'ACT', 'IZ'])")
        self.assertEqual(U.phrases("Invented Zebra Standard", ["IZS", "ACT", "IZ", "izs"], {"act"}), ["Invented Zebra Standard", "IZS"])
        got = sorted(U.count_entry(cur, ["Invented Zebra Standard", "IZS"])); self.assertEqual(got, [(2024, 1, 1), (2025, 2, 1)])
        self.assertEqual(U.count_entry(cur, []), [])
        # an acronym that is an everyday word: "ACT" is on the page as "act", "IZS" as written
        self.assertEqual((U.is_acronym("IZS"), U.is_acronym("SBTi"), U.is_acronym("Invented Zebra Standard"), U.is_acronym("Act")), (True, True, False, False))
        self.assertEqual((U.written_share(cur, "IZS"), U.written_share(cur, "ACT"), U.written_share(cur, "QQQZ")), (1.0, 0.0, 1.0))
        self.assertEqual(U.checked(cur, ["Invented Zebra Standard", "IZS", "ACT"]), ["Invented Zebra Standard", "IZS"])
        cur.executemany("insert into public.reference_use (entry, year, documents, companies) values ('norm:izs', %s, %s, %s)", got)
        cur.execute("select public.admin_reference_use('k')"); r = cur.fetchone()[0]
        self.assertEqual((r["year"], r["documents"], r["companies"], r["years"]), (2025, 3, 2, [2025, 2024]))
        self.assertEqual([(x["name"], x["documents"], x["companies"], x["before"]) for x in r["rows"]], [("Invented Zebra Standard", 2, 1, 1)])
        self.assertEqual(r["companies_before"], 1)
        cur.execute("select public.admin_reference_use_series('k', 'norm:izs')"); s = {v["year"]: (v["companies"], v["of"]) for v in cur.fetchone()[0]}
        self.assertEqual((s[2024], s[2025]), ((1, 1), (1, 2)))
        cur.execute("insert into public.documents (library_id, wba_id, company, doc_type, year, label, pages, text_status) values ('RU5', 'RUC', 'C', 'UNKNOWN', 2025, 'e', 1, 'ok')")
        cur.execute("insert into public.pages (library_id, page, body) values ('RU5', 1, 'Invented Zebra Standard, in a file of no known kind')")
        self.assertEqual(sorted(U.count_entry(cur, ["Invented Zebra Standard"])), [(2024, 1, 1), (2025, 1, 1)])       # the untyped file is not counted
        cur.execute("select public.admin_reference_use('k')"); self.assertEqual(cur.fetchone()[0]["companies"], 2)
        cur.execute("insert into public.reference_entries (id, kind, name, name_key) values ('regulation:izs2', 'regulation', 'Invented Zebra Standards', 'invented zebra standards')")      # the same norm held twice
        cur.execute("insert into public.reference_use (entry, year, documents, companies) values ('regulation:izs2', 2025, 2, 1)")
        cur.execute("select public.admin_reference_use('k')"); self.assertEqual([x["kind"] for x in cur.fetchone()[0]["rows"]], ["norm"])
        cur.execute("delete from public.reference_entries where id = 'regulation:izs2'")
        cur.execute("select public.admin_reference_use('k', 2024, 'regulation')"); self.assertEqual(cur.fetchone()[0]["rows"], [])
        cur.execute("delete from public.reference_use"); cur.execute("delete from public.reference_entries where id = 'norm:izs'")
        cur.execute("delete from public.pages where library_id like 'RU_'"); cur.execute("delete from public.documents where library_id like 'RU_'")

    def test_a_company_is_known_by_its_name_without_the_legal_form(self):
        """23 Nov 2026: "porsche" did not name "Porsche AG". The short name is worked out by rule, a one-word one is
        judged on the pages, and the company list gives the safe ones to the page."""
        import company_short_names as C
        self.assertEqual([C.short_of(n) for n in ("Zuffenhaus AG", "Northern Zebra Motors Company", "Bank of Zebra Limited", "The Zebra-Cola Company", "Zebra", "Zeb Inc.")],
                         ["Zuffenhaus", "Northern Zebra Motors", "Bank of Zebra", "Zebra-Cola", None, None])
        self.assertEqual(C.plan([("A", "Zuffenhaus AG"), ("B", "Zuffenhaus Automobil Holding SE"), ("C", "Striped Corporation"), ("D", "Striped"), ("E", "Twin plc"), ("F", "Twin Inc.")]),
                         [("A", "Zuffenhaus", 1), ("B", "Zuffenhaus Automobil", 2)])       # a short name another company has, as short or listed name, is nobody's
        self.assertEqual([C.one_word_safe(*x) for x in ((900, 0), (30000, 9000), (30000, 300), (70000, 60000), (0, 0))], [True, True, False, False, True])
        cur = self.conn.cursor()
        cur.execute("insert into public.companies (wba_id, name) values ('SN1', 'Zuffenhaus AG'), ('SN2', 'Everyday Corporation') on conflict do nothing")
        cur.execute("insert into public.company_short_names (wba_id, short, pages, own_pages, safe) values ('SN1', 'Zuffenhaus', 900, 400, true), ('SN2', 'Everyday', 70000, 10, false)")
        cur.execute("select public.find_companies('k')"); got = {c["id"]: c.get("short") for c in cur.fetchone()[0] if c["id"] in ("SN1", "SN2")}
        self.assertEqual(got, {"SN1": "Zuffenhaus", "SN2": None})
        cur.execute("delete from public.company_short_names"); cur.execute("delete from public.companies where wba_id in ('SN1', 'SN2')")

    def test_what_the_reports_name_and_the_list_lacks_becomes_a_candidate(self):
        """23 Nov 2026: reports write a full name and its acronym in brackets. A pair several companies write and the
        list does not hold is a candidate; a company's own name is not; adding the entry closes the candidate."""
        import reference_candidates as C
        page = ("We follow the Task Force on Zebra-related Financial Disclosures (TZFD) and count zebrahouse gas (ZHG) emissions. Zebra Motors AG (ZM) and the "
                "Invented Zebra Reporting Directive (IZRD) apply; the Zebra Advisory Council (ZAC) agrees. See NOTE (AB).")
        self.assertEqual(C.pairs(page), [("Task Force on Zebra-related Financial Disclosures", "TZFD"), ("zebrahouse gas", "ZHG"), ("Zebra Motors AG", "ZM"), ("Invented Zebra Reporting Directive", "IZRD"), ("Zebra Advisory Council", "ZAC")])     # the company's own name is left out by candidates()
        self.assertEqual([C.guess(n) for n in ("Invented Zebra Reporting Directive", "Zebra Advisory Council", "Zebra Accounting Standard", "zebrahouse gas")], ["regulation", "organisation", "norm", "term"])
        rows = [(f"L{i}", f"W{i % 4}", page) for i in range(8)] + [("L9", "W9", "Our own Zebra Excellence Programme (ZEP) is ours alone.")]
        got = C.candidates(rows, held={"izrd"}, company_keys={"zebra advisory council"})
        self.assertEqual(sorted((g[1], g[2], g[3], g[4], g[5]) for g in got), [("Task Force on Zebra-related Financial Disclosures", "TZFD", "term", 4, 8), ("zebrahouse gas", "ZHG", "term", 4, 8)])
        cur = self.conn.cursor()
        cur.executemany("insert into public.reference_candidates (key, name, acronym, kind, companies, documents, library_id) values (%s, %s, %s, %s, %s, %s, %s)", got)
        cur.execute("insert into public.reference_entries (id, kind, name, name_key) values ('organisation:tzfd', 'organisation', 'TZFD Secretariat', 'tzfd secretariat')")
        cur.execute("select public.admin_reference_candidates('k')"); r = cur.fetchone()[0]; self.assertEqual((r["found"], len(r["rows"])), (2, 2))
        self.assertEqual({x["acronym"]: x["near"] for x in r["rows"]}, {"TZFD": ["TZFD Secretariat (organisation)"], "ZHG": []})      # what the list holds that looks like it
        cur.execute("delete from public.reference_entries where id = 'organisation:tzfd'")
        cur.execute("select public.admin_reference_candidate_set('k', 'zebrahouse gas', true)"); self.assertTrue(cur.fetchone()[0]["ok"])
        cur.execute("select public.admin_reference_save('k', null, 'norm', 'Task Force on Zebra-related Financial Disclosures', array['TZFD'], 'Invented.')"); made = cur.fetchone()[0]["id"]
        cur.execute("select key, status from public.reference_candidates order by key"); self.assertEqual(cur.fetchall(), [("task force on zebra related financial disclosures", "added"), ("zebrahouse gas", "dismissed")])
        cur.execute("select public.admin_reference_candidates('k')"); self.assertEqual(cur.fetchone()[0]["rows"], [])
        cur.execute("delete from public.reference_candidates"); cur.execute("delete from public.reference_entries where id = %s", (made,))

    def test_the_register_of_legal_entities_says_who_a_company_is_and_whose(self):
        """24 Nov 2026: GLEIF's register, asked for the companies that carry an LEI. A hundred LEIs a request, a parent
        asked once and only where the record says one exists, never faster than the limit; the owner sees the LEIs
        that are unknown or lapsed and the companies that are another's subsidiary."""
        import gleif_register as G
        rec = lambda lei, name, status="ACTIVE", reg="ISSUED", dp=None, up=None: {"id": lei, "attributes": {"lei": lei, "entity": {"legalName": {"name": name}, "legalAddress": {"country": "GB"}, "status": status},
                                                                                         "registration": {"status": reg}}, "relationships": {"direct-parent": {"links": dp or {}}, "ultimate-parent": {"links": up or {}}}}
        A, B, C, P = "A" * 18 + "11", "B" * 18 + "22", "C" * 18 + "33", "P" * 18 + "44"
        book = {A: rec(A, "Zebra Bank UK plc", dp={"lei-record": "u/P"}, up={"lei-record": "u/P"}), B: rec(B, "Zebra Mills SA", reg="LAPSED", up={"reporting-exception": "u/x"})}
        class Answer:
            def __init__(self, code, body=None): self.status_code, self._b = code, body
            def json(self): return self._b
        class Sess:
            def __init__(self): self.asked = []
            def get(self, url, params=None, headers=None, timeout=None):
                self.asked.append((url, dict(params or {}), headers["User-Agent"]))
                if url.endswith("/lei-records"):
                    return Answer(200, {"data": [book[l] for l in params["filter[lei]"].split(",") if l in book]})
                return Answer(200, {"data": rec(P, "Zebra Group SA")}) if url == "u/P" else Answer(404)
        waits, now = [], [0.0]
        sess = Sess(); ask = G.Asker(sess, "the-bot", per_minute=60, sleep=lambda s: (waits.append(s), now.__setitem__(0, now[0] + s)), clock=lambda: now[0])
        keys = set(); rows = G.read(ask, [("W1", A), ("W2", B), ("W3", C), ("W4", A)], keys)
        self.assertEqual((rows["W1"]["name"], rows["W1"]["ultimate_lei"], rows["W1"]["ultimate_name"], rows["W1"]["direct_name"]), ("Zebra Bank UK plc", P, "Zebra Group SA", "Zebra Group SA"))
        self.assertEqual((rows["W2"]["registration_status"], rows["W2"]["ultimate_kind"], rows["W2"].get("ultimate_lei")), ("LAPSED", "exception", None))
        self.assertEqual((rows["W3"]["found"], rows["W4"]["lei"]), (False, A))                         # an LEI the register does not know; two companies with one LEI
        self.assertEqual([u for u, _, _ in sess.asked], [G.API + "/lei-records", "u/P"])               # one request for the records, the parent once
        self.assertEqual({ua for _, _, ua in sess.asked}, {"the-bot"}); self.assertTrue(all(w >= 0.99 for w in waits[1:]))   # by name, a second apart at 60 a minute
        self.assertIn("ultimate-parent.links.reporting-exception", keys)
        # the other way: the entities below a parent, 200 a request, at most the cap, with the register's own count; a name in
        # another script brings its Latin one, and former names are kept
        import gleif_children as K
        kid = lambda n, name, other=None, trans=None: {"id": f"K{n:019d}", "attributes": {"lei": f"K{n:019d}", "entity": {"legalName": {"name": name}, "legalAddress": {"country": "JP"}, "status": "ACTIVE",
                                                                                                          "otherNames": other or [], "transliteratedOtherNames": trans or []}}}
        class Sess2:
            def __init__(self): self.asked = []
            def get(self, url, params=None, headers=None, timeout=None):
                self.asked.append((url.rsplit("/", 1)[-1], int(params["page[number]"])))
                if url.endswith("/direct-children"):
                    return Answer(200, {"meta": {"pagination": {"total": 2, "lastPage": 1}}, "data": [kid(1, "三菱ゼブラ株式会社", trans=[{"name": "Mitsu Zebra KK", "type": "PREFERRED_ASCII_TRANSLITERATED_LEGAL_NAME"}]),
                                                                                                     kid(2, "Zebra Leasing Ltd", other=[{"name": "Old Zebra Leasing", "type": "PREVIOUS_LEGAL_NAME"}, {"name": "ZL", "type": "TRADING_OR_OPERATING_NAME"}])]})
                n = int(params["page[number]"]); return Answer(200, {"meta": {"pagination": {"total": 450, "lastPage": 3}}, "data": [kid(100 * n + i, f"Zebra Unit {n}-{i}") for i in range(200 if n < 3 else 50)]})
        s2 = Sess2(); ask2 = G.Asker(s2, "the-bot", per_minute=6000, sleep=lambda s: None)
        direct, d_total = K.children(ask2, P, "direct-children"); deep, u_total = K.children(ask2, P, "ultimate-children", cap=300)
        self.assertEqual((d_total, [(r["name_latin"], r["former"]) for r in direct]), (2, [("Mitsu Zebra KK", []), (None, ["Old Zebra Leasing"])]))
        self.assertEqual((u_total, len(deep), s2.asked), (450, 300, [("direct-children", 1), ("ultimate-children", 1), ("ultimate-children", 2)]))   # the cap stops the asking; the total is the register's
        self.assertEqual((K.latin("三菱ゼブラ株式会社"), K.latin("Société Zèbre SA")), (False, True))
        # below the first level: each entity is asked for its own children, level by level, until all have their place or the budget is spent
        fam = {"F1": ["G1", "G2"], "F2": [], "G1": ["H1"], "G2": [], "H1": []}
        class Sess3:
            def __init__(self): self.asked = []
            def get(self, url, params=None, headers=None, timeout=None):
                lei = url.split("/")[-2]; self.asked.append(lei)
                return Answer(200, {"meta": {"pagination": {"total": len(fam[lei]), "lastPage": 1}}, "data": [{"id": k, "attributes": {"lei": k, "entity": {"legalName": {"name": "Zebra " + k}}}} for k in fam[lei]]})
        # the branches come from the register's own file of relationships: one download, read in a stream; only active
        # relationships of the entities known here; the link is found in the index by its shape
        import gleif_relations as R, tempfile, zipfile as zf
        self.assertEqual(R.csv_link({"data": {"publish_date": "x", "full_file": {"xml": {"url": "u.xml"}, "csv": {"size": 1, "url": "https://x/rr.csv.zip"}}, "delta_files": {"LastDay": {"csv": {"url": "d"}}}}}), "https://x/rr.csv.zip")
        self.assertIsNone(R.csv_link({"data": {"delta_files": {"csv": {"url": "d"}}}}))
        text = "\ufeff" + ",".join([R.START, R.END, R.TYPE, R.STATUS, "Other"]) + "\n" + "\n".join(["a1,P1,%s,ACTIVE,x" % R.DIRECT, "A1,U1,%s,ACTIVE,x" % R.ULTIMATE, "A2,P9,%s,INACTIVE,x" % R.DIRECT, "ZZ,P1,%s,ACTIVE,x" % R.DIRECT, "A3,A3,%s,ACTIVE,x" % R.DIRECT]) + "\n"
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rr.zip")
            with zf.ZipFile(path, "w") as z: z.writestr("20261124-rr.csv", text)
            rows, cols = R.open_rows(path); direct, ultimate, cnt = R.parents(rows, {"A1", "A2", "A3"})
        self.assertEqual((cols[0], direct, ultimate, sum(cnt.values())), (R.START, {"A1": "P1"}, {"A1": "U1"}, 5))          # an inactive one, one of an entity not known here, and an entity under itself are left
        # every entity of the groups held, not the first 600 of a parent: who belongs to a group, and its names from the file of entities
        mem = R.members_of({"X1": "T1", "X2": "X1", "X3": "T9", "X4": "T1"}, {"X1": "T1", "X2": "T1", "X3": "T9"}, {"T1"})
        self.assertEqual(mem, {"X1": ("T1", "T1"), "X2": ("X1", "T1"), "X4": ("T1", None)})
        ecols = ["LEI", "Entity.LegalName", "Entity.OtherEntityNames.OtherEntityName.1", "Entity.OtherEntityNames.OtherEntityName.1.type", "Entity.TransliteratedOtherEntityNames.TransliteratedOtherEntityName.1",
                 "Entity.LegalAddress.Country", "Entity.EntityStatus", "Entity.SuccessorEntity.1.SuccessorLEI"]
        row = dict(zip(ecols, ["X1", "三菱ゼブラ株式会社", "Old Zebra KK", "PREVIOUS_LEGAL_NAME", "Mitsu Zebra KK", "JP", "ACTIVE", "x9"]))
        self.assertEqual(R.entity_row(row, ecols), ("三菱ゼブラ株式会社", "Mitsu Zebra KK", ["Old Zebra KK"], "JP", "ACTIVE", "X9"))
        self.assertEqual(R.entity_row(dict(zip(ecols, ["X2", "Zebra Ltd", "", "", "", "GB", "ACTIVE", ""])), ecols), ("Zebra Ltd", None, [], "GB", "ACTIVE", None))
        # 24 Nov 2026: main() named a list `deep` and so hid the function of that name: the first run of the levels died at once
        import types
        for mod in (K, G, R):
            self.assertFalse(set(mod.main.__code__.co_varnames) & {n for n, v in vars(mod).items() if isinstance(v, types.FunctionType)}, mod.__name__)
        s3 = Sess3(); found, used = K.walk(G.Asker(s3, "the-bot", per_minute=6000, sleep=lambda s: None), ["F1", "F2"], 5)
        self.assertEqual(([(r["lei"], p) for r, p in found], used, s3.asked), ([("G1", "F1"), ("G2", "F1"), ("H1", "G1")], 3, ["F1", "F2", "G1"]))     # all five placed: G2 and H1 are not asked
        s3 = Sess3(); found, used = K.walk(G.Asker(s3, "the-bot", per_minute=6000, sleep=lambda s: None), ["F1", "F2"], 5, budget=1)
        self.assertEqual(([r["lei"] for r, _ in found], used), (["G1", "G2"], 1))                                                                    # the budget stops the walk
        cur = self.conn.cursor()
        cur.execute("create table if not exists public.companies (wba_id text primary key, name text, industry text, country text, region text, website text, lantern_id text, lei text)")
        cur.execute("alter table public.companies add column if not exists lei text")
        cur.executemany("insert into public.companies (wba_id, name, lei) values (%s, %s, %s) on conflict (wba_id) do update set name = excluded.name, lei = excluded.lei", [("RG1", "Zebra Bank UK", A), ("RG2", "Zebra Mills", B), ("RG3", "Zebra Lost", C), ("RG4", "Zebra Fine", P)])
        put = lambda w, lei, found, name=None, st=None, reg=None, ul=None, un=None: cur.execute(
            "insert into public.company_register (wba_id, lei, found, legal_name, entity_status, registration_status, ultimate_parent_lei, ultimate_parent_name) values (%s, %s, %s, %s, %s, %s, %s, %s)", (w, lei, found, name, st, reg, ul, un))
        put("RG1", A, True, "Zebra Bank UK plc", "ACTIVE", "ISSUED", P, "Zebra Group SA"); put("RG2", B, True, "Zebra Mills SA", "ACTIVE", "LAPSED"); put("RG3", C, False); put("RG4", P, True, "Zebra Group SA", "ACTIVE", "ISSUED", P, "Zebra Group SA")
        cur.execute("select public.admin_register('k')"); r = cur.fetchone()[0]
        put("RG5", "D" * 18 + "55", True, "Zebra Old SA", "ACTIVE", "RETIRED")
        cur.execute("update public.company_register set entity_status = 'NULL' where wba_id = 'RG4'")          # the register's word for no status: not trouble
        cur.execute("update public.company_register set successor_lei = %s, successor_name = 'Zebra Fine SA' where wba_id = 'RG3'", (P,))
        cur.execute("select public.admin_register('k')"); r = cur.fetchone()[0]
        self.assertEqual([(x["successor_lei"], x["successor_name"]) for x in r["rows"] if x["wba_id"] == "RG3"], [(P, "Zebra Fine SA")])          # the LEI the register kept in its place is offered on the row
        self.assertEqual((r["checked"], r["fine"], r["lapsed"]), (5, 2, 1))                           # a lapsed registration is counted, not listed: the LEI was not renewed
        self.assertEqual([(x["wba_id"], x["what"]) for x in r["rows"]], [("RG3", "not an LEI: its check digits do not fit"), ("RG1", "a subsidiary: its ultimate parent is another entity")])   # RG5 has no company row; the test's made-up codes are not LEIs
        # the owner corrects an LEI: check digits, no other company's, kept apart from the nightly copy, asked again at the next reading
        good, cut = "95980020140005374753", "95980020140005300000"
        cur.execute("select public.lei_ok(%s), public.lei_ok(%s), public.lei_ok('549300772D1G8JPIUR96'), public.lei_ok(null), public.lei_ok('short')", (good, cut)); self.assertEqual(cur.fetchone(), (True, False, True, False, False))
        cur.execute("update public.companies set lei = %s where wba_id = 'RG3'", (cut,)); cur.execute("update public.company_register set lei = %s where wba_id = 'RG3'", (cut,))
        cur.execute("select public.admin_register('k')"); self.assertIn("a spreadsheet rounded it", [x["what"] for x in cur.fetchone()[0]["rows"] if x["wba_id"] == "RG3"][0])
        cur.execute("select public.admin_register_lei_set('k', 'RG3', %s)", (cut,)); self.assertFalse(cur.fetchone()[0]["ok"])
        cur.execute("select public.admin_register_lei_set('k', 'RG3', %s)", (A,)); self.assertFalse(cur.fetchone()[0]["ok"])                      # not an LEI: the test's letters do not pass the check
        cur.execute("select public.admin_register_lei_set('k', 'RG3', ' 9598 0020 1400 0537 4753 ')"); self.assertEqual(cur.fetchone()[0], {"ok": True, "lei": good})
        cur.execute("select public.admin_register_lei_set('k', 'RG2', %s)", (good,)); self.assertIn("another company has that LEI", cur.fetchone()[0]["why"])
        cur.execute("select lei from public.companies where wba_id = 'RG3'"); self.assertEqual(cur.fetchone(), (good,))
        cur.execute("select lei, was from public.company_lei_fixes where wba_id = 'RG3'"); self.assertEqual(cur.fetchone(), (good, cut))
        cur.execute("select public.admin_register('k')"); r = cur.fetchone()[0]
        self.assertEqual([x["what"] for x in r["rows"] if x["wba_id"] == "RG3"], ["corrected: the register is asked at its next reading (daily)"])
        cur.execute("select wba_id from public.company_register order by checked_at, wba_id limit 1"); self.assertEqual(cur.fetchone(), ("RG3",))   # first at the next reading
        cur.execute("insert into public.companies (wba_id, name) values ('RG6', 'Zebra No Code')"); cur.execute("select public.admin_register('k')")
        self.assertIn("RG6", [x["wba_id"] for x in cur.fetchone()[0]["no_lei"]])
        cur.execute("update public.companies set lei = %s where wba_id = 'RG3'", (C,)); cur.execute("update public.company_register set lei = %s, found = false, pending = false where wba_id = 'RG3'", (C,)); cur.execute("delete from public.company_lei_fixes")
        # one company's group: its parents, the entities one level below (a Latin name first), the totals, and its sisters
        cur.execute("update public.company_register set direct_parent_lei = %s, direct_parent_name = 'Zebra Group SA' where wba_id = 'RG1'", (P,))
        cur.executemany("insert into public.register_entities (lei, name, name_latin, country, direct_parent_lei, ultimate_parent_lei) values (%s, %s, %s, %s, %s, %s)",
                        [("K1", "三菱ゼブラ株式会社", "Mitsu Zebra KK", "JP", A, P), ("K2", "Zebra Leasing Ltd", None, "GB", A, P), ("K3", "Zebra Deep Ltd", None, "GB", "K2", P), (A, "Zebra Bank UK plc", None, "GB", P, P), ("S1", "Zebra Brasil SA", None, "BR", P, P)])
        cur.execute("insert into public.register_groups (lei, direct_total, ultimate_total, direct_kept, ultimate_kept) values (%s, 2, 3, 2, 3)", (A,))
        cur.execute("select public.admin_register_group('k', 'zebra bank')"); g = cur.fetchone()[0]
        self.assertEqual((g["found"], g["company"]["name"], g["ultimate_parent"]["name"], g["ultimate_parent"]["held"], g["direct_parent"]["lei"]), (True, "Zebra Bank UK", "Zebra Group SA", "Zebra Fine", P))
        self.assertEqual(([k["name"] for k in g["children"]], g["direct_total"], g["ultimate_total"]), (["Mitsu Zebra KK", "Zebra Leasing Ltd"], 2, 3))
        self.assertEqual(([s["name"] for s in g["sisters"]], g["sisters_total"]), (["Zebra Brasil SA"], 1))                                   # itself is not its own sister
        self.assertEqual([(x["name"], x["level"]) for x in g["all"]], [("Mitsu Zebra KK", "one level below"), ("Zebra Leasing Ltd", "one level below")])
        # an entity lantrNa does not hold can be the centre: by its LEI. A child, then the parent itself
        cur.execute("select public.admin_register_group('k', 'K2')"); self.assertEqual(cur.fetchone()[0], {"found": False})                  # not an LEI
        k2 = "K" + "2" * 17 + "22"; cur.execute("update public.register_entities set lei = %s where lei = 'K2'", (k2,)); cur.execute("update public.register_entities set direct_parent_lei = %s where lei = 'K3'", (k2,))
        cur.execute("select public.admin_register_group('k', %s)", (k2.lower(),)); e = cur.fetchone()[0]
        self.assertEqual((e["company"]["name"], e["company"]["held"], e["direct_parent"]["held"], e["ultimate_parent"]["name"], [k["name"] for k in e["children"]], [s["name"] for s in e["sisters"]]),
                         ("Zebra Leasing Ltd", False, "Zebra Bank UK", "Zebra Group SA", ["Zebra Deep Ltd"], ["Mitsu Zebra KK"]))
        cur.execute("select public.admin_register_group('k', 'zebra bank')"); tr = cur.fetchone()[0]["tree"]
        self.assertEqual([(x["name"], x["depth"], x["parent"] == A or x["parent"] == k2) for x in tr], [("Mitsu Zebra KK", 1, True), ("Zebra Leasing Ltd", 1, True), ("Zebra Deep Ltd", 2, True)])   # the branches: a child of a child
        cur.execute("select public.admin_register_group('k', %s)", (P,)); top = cur.fetchone()[0]
        self.assertEqual((top["company"]["held"], top["company"]["name"], sorted(k["name"] for k in top["children"])), (True, "Zebra Fine", ["Zebra Bank UK plc", "Zebra Brasil SA"]))
        # the names a company is also reached by: a subsidiary's, a former one, the legal one; two words at least; a search with no company is offered the company
        cur.execute("update public.company_register set former_names = array['Old Zebra Banking Corporation', 'Zebra'], legal_name = 'Zebra Fine Trading SA' where wba_id = 'RG4'")
        cur.execute("update public.register_entities set former_names = array['Stripe Lease Partners'] where lei = %s", (k2,))
        cur.execute("select public.register_names_refresh()"); self.assertGreaterEqual(cur.fetchone()[0], 5)
        cur.execute("select key, wba_id, kind from public.register_names order by key")
        got = {k: (w, kind) for k, w, kind in cur.fetchall()}
        self.assertEqual(got["zebra leasing"], ("RG4", "a subsidiary")); self.assertEqual(got["stripe lease partners"], ("RG4", "a former name of a subsidiary"))
        self.assertEqual(got["old zebra banking"], ("RG4", "a former name")); self.assertEqual(got["zebra fine trading"], ("RG4", "its legal name"))
        self.assertNotIn("zebra", got); self.assertNotIn("zebra bank uk", got)                      # one word; and a subsidiary lantrNa holds is its own company, reached by its own name
        ask_e = lambda q: (cur.execute("select public.find_entity('k', %s)", (q,)), cur.fetchone()[0])[1]
        self.assertEqual(ask_e("scope 1 emissions of Zebra Leasing Ltd in 2024"), {"id": "RG4", "company": "Zebra Fine", "name": "Zebra Leasing Ltd", "kind": "a subsidiary"})
        self.assertEqual(ask_e("Old Zebra Banking Corporation revenue")["kind"], "a former name")
        self.assertIsNone(ask_e("water use in 2024")); self.assertIsNone(ask_e("zebra")); self.assertIsNone(ask_e(""))
        cur.execute("delete from public.register_names")
        cur.execute("select public.admin_register_group('k', 'no such company')"); self.assertEqual(cur.fetchone()[0], {"found": False})
        cur.execute("delete from public.register_entities"); cur.execute("delete from public.register_groups")
        cur.execute("select public.admin_register_all('k')"); al = {x["wba_id"]: x for x in cur.fetchone()[0]}
        self.assertEqual(len(al), 5); self.assertEqual((al["RG1"]["company"], al["RG1"]["ultimate_parent_name"], al["RG1"]["held_parent"]), ("Zebra Bank UK", "Zebra Group SA", "Zebra Fine"))   # its parent is a company of lantrNa too
        self.assertEqual((al["RG4"]["held_parent"], al["RG3"]["found"], al["RG5"]["company"]), (None, False, None))
        cur.execute("delete from public.company_register"); cur.execute("delete from public.companies where wba_id like 'RG_'")

    def test_the_catalogue_is_counted_from_the_registers_files(self):
        """24 Nov 2026: before a list of companies of lantrNa's own is built, how many it would hold: entities with an
        ISIN that are companies (not funds), active, and nobody's subsidiary."""
        import catalogue_count as CC
        cols = ["LEI", "Entity.LegalName", "Entity.LegalAddress.Country", "Entity.EntityCategory", "Entity.EntityStatus"]
        rows = [dict(zip(cols, x)) for x in [("A", "a", "ES", "GENERAL", "ACTIVE"), ("B", "b", "ES", "FUND", "ACTIVE"), ("C", "c", "GB", "GENERAL", "INACTIVE"), ("D", "d", "GB", "GENERAL", "ACTIVE"), ("E", "e", "FR", "", "ACTIVE"), ("F", "f", "FR", "GENERAL", "ACTIVE")]]
        c, by, used = CC.count(rows, cols, {"A", "B", "C", "D", "E"}, {"D"})
        self.assertEqual((c["entities"], c["with_an_isin"], c["companies_active_with_an_isin"], c["of_them_subsidiaries"], c["catalogue"], dict(by)), (6, 5, 3, 1, 2, {"ES": 1, "FR": 1}))
        self.assertEqual(used["country"], "Entity.LegalAddress.Country")
        self.assertEqual(CC.any_link({"data": {"attributes": {"fileName": "x.zip", "downloadLink": "https://m.example/isin-lei/abc/download"}}}), "https://m.example/isin-lei/abc/download")
        self.assertIsNone(CC.any_link({"data": {"attributes": {"fileName": "x.zip", "page": "https://m.example/about"}}}))

    def test_a_document_is_told_as_a_subsidiarys_by_its_address_or_label(self):
        """24 Nov 2026: a bank's UK subsidiary's annual report was held under the group, labelled "Annual Report 2025";
        its address named the subsidiary. The longest subsidiary name in the label (whole words) or in the address
        (spaces out) tells it; the company's own name, or a short name in an address, tells nothing."""
        import subsidiary_documents as SD
        names = [("zebra uk", "Zebra UK Group Holdings plc"), ("zebra", "Zebra SA"), ("zebra leasing", "Zebra Leasing Ltd"), ("banco zebra totta", "Banco Zebra Totta SA"), ("zb fin", "ZB Fin Ltd")]
        own = SD.fold("Banco Zebra")
        self.assertEqual(SD.told_by("2025 | Banco Zebra | Annual Report 2025", "https://www.zebra.co.uk/assets/ZebraLeasingLtd2025AnnualReport.pdf", names, own), ("Zebra Leasing Ltd", "address"))
        self.assertEqual(SD.told_by("2024 | Banco Zebra | Zebra UK annual report", "https://x.example/a.pdf", names, own), ("Zebra UK Group Holdings plc", "label"))
        self.assertEqual(SD.told_by("Relatório Banco Zebra Totta 2024", "https://x.example/zebrauk.pdf", names, own), ("Banco Zebra Totta SA", "label"))     # the longest; "zebrauk" is too short for an address
        self.assertIsNone(SD.told_by("2024 | Banco Zebra | Annual Report", "https://www.bancozebra.com/zbfin/annual-report-2024.pdf", names, own))
        cur = self.conn.cursor()
        cur.execute("insert into public.document_entities (library_id, name, kind, how) values ('DE1', 'Zebra Leasing Ltd', 'a subsidiary', 'address')")
        cur.execute("select name, how from public.document_entities where library_id = 'DE1'"); self.assertEqual(cur.fetchone(), ("Zebra Leasing Ltd", "address"))
        cur.execute("insert into public.documents (library_id, wba_id, company, label, text_status) values ('DE1', 'WX', 'Banco Zebra', 'Annual Report', 'ok'), ('DE2', 'WX', 'Banco Zebra', 'Annual Report', 'ok')")
        cur.execute("select public.doc_facts('k', array['DE1', 'DE2'])"); f = cur.fetchone()[0]; self.assertEqual((f["DE1"]["entity"], f["DE2"]["entity"]), ("Zebra Leasing Ltd", None))   # the page says it on the row and under an answer
        # option ask_source_entity: the reader is told so in the page's heading, only with the option on
        cur.execute("select public.ask_doc_note('DE1')"); self.assertEqual(cur.fetchone()[0], "")
        cur.execute("select set_config('lantern.ask_source_entity', 'on', false)")
        cur.execute("select public.ask_doc_note('DE1'), public.ask_doc_note('DE2')")
        self.assertEqual(cur.fetchone(), (" (this document is a report of Zebra Leasing Ltd, a subsidiary of the company, not of the whole group)", ""))
        cur.execute("select set_config('lantern.ask_source_entity', '', false)")
        cur.execute("delete from public.documents where library_id in ('DE1', 'DE2')")
        cur.execute("delete from public.document_entities")

    def test_what_a_standard_setter_names_on_its_page_and_the_list_lacks_is_a_candidate(self):
        """24 Nov 2026: the list's entries carry the address of their own page. A full name with its acronym found there
        and not held by the list is a candidate with the site that names it; a page robots.txt forbids, or that the
        site refuses, is not read and no other name is tried; the reports' weekly run leaves these candidates alone."""
        import standards_watch as W, robot_identity as bot
        from reference_candidates import pairs, key_of, LEGAL
        page = ("<html><head><script>var a = 'Hidden Zebra Body (HZB)';</script><style>.x{}</style></head><body><p>The Zebra Sustainability Standards Board&nbsp;(ZSSB) "
                "works with the <a href='/x'>Held Zebra Council (HZC)</a> and Zebra Motors AG (ZM).</p></body></html>")
        self.assertEqual(W.found_on(page, {"hzc"}, pairs, key_of, LEGAL), {"zebra sustainability standards board": ("Zebra Sustainability Standards Board", "ZSSB")})
        self.assertEqual((W.host_of("https://www.Example.org/a?b"), W.host_of("http://sub.example.org")), ("example.org", "sub.example.org"))

        class Answer:
            def __init__(self, code, body=b"", kind="text/html; charset=utf-8"):
                import io
                self.status_code, self.headers, self.encoding, self.raw, self.text = code, {"Content-Type": kind}, "utf-8", None, body.decode()
                raw = io.BytesIO(body); self.raw = type("R", (), {"read": staticmethod(lambda n, decode_content=True: raw.read(n))})
            def close(self): pass
        class Sess:
            def __init__(self): self.asked = []
            def get(self, url, headers=None, **kw):
                self.asked.append((url, headers["User-Agent"]))
                if url.endswith("/robots.txt"):
                    return Answer(200, b"User-agent: *\nDisallow: /private/\n", "text/plain") if "closed.example" in url else Answer(404)
                return Answer(403) if "refuses.example" in url else Answer(200, b"%PDF", "application/pdf") if url.endswith(".pdf") else Answer(200, page.encode())
        s, robots = Sess(), {}
        self.assertEqual(W.read(s, "https://open.example/about", robots, bot)[0], "ok")
        self.assertEqual(W.read(s, "https://closed.example/private/a", robots, bot), ("forbidden", ""))
        self.assertEqual(W.read(s, "https://refuses.example/a", robots, bot), ("refused", ""))
        self.assertEqual(W.read(s, "https://open.example/a.pdf", robots, bot), ("not html", ""))
        self.assertEqual({ua for _, ua in s.asked}, {bot.BOT_UA})                                   # by its own name, always
        self.assertNotIn("https://closed.example/private/a", [u for u, _ in s.asked])              # a forbidden page is never asked for
        self.assertEqual(len([u for u, _ in s.asked if "refuses.example/a" in u]), 1)               # refused once, not tried again
        self.assertEqual(len([u for u, _ in s.asked if u == "https://open.example/robots.txt"]), 1) # robots.txt once a site

        cur = self.conn.cursor()
        cur.execute("insert into public.reference_candidates (key, name, acronym, kind, companies, documents, library_id) values ('from the reports', 'From The Reports', 'FTR', 'term', 5, 9, 'L1')")
        cur.execute("insert into public.reference_candidates (key, name, acronym, kind, source, seen_on, new_on_site) values ('zebra sustainability standards board', 'Zebra Sustainability Standards Board', 'ZSSB', 'organisation', 'setter', array['open.example'], true)")
        cur.execute("insert into public.reference_watch (url, entry, status, names) values ('https://open.example/about', 'organisation:x', 'ok', array['zebra sustainability standards board']), ('https://closed.example/private/a', 'norm:y', 'forbidden', '{}')")
        cur.execute("select public.admin_reference_candidates('k')"); r = cur.fetchone()[0]
        self.assertEqual(([x["key"] for x in r["rows"]], [(x["name"], x["seen_on"], x["new"]) for x in r["setters"]]), (["from the reports"], [("Zebra Sustainability Standards Board", ["open.example"], True)]))
        self.assertEqual((r["watch"]["pages"], r["watch"]["read"], r["watch"]["closed"]), (2, 1, 1))
        # the addresses that could not be read, with the entries that carry them: gone before closed, a read page never
        cur.execute("insert into public.reference_entries (id, kind, name, name_key, url) values ('norm:gonez', 'norm', 'Gone Zebra Standard', 'gone zebra standard', 'https://gone.example/s')")
        cur.execute("insert into public.reference_watch (url, entry, status) values ('https://gone.example/s', 'norm:gonez', 'gone')")
        cur.execute("select public.admin_reference_candidates('k')"); un = cur.fetchone()[0]["unread"]
        self.assertEqual([(x["url"], x["status"], x["entries"]) for x in un], [("https://gone.example/s", "gone", ["Gone Zebra Standard (norm)"]), ("https://closed.example/private/a", "forbidden", [])])
        cur.execute("delete from public.reference_entries where id = 'norm:gonez'")
        cur.execute("delete from public.reference_candidates where status = 'found' and source = 'reports'")          # what reference_candidates.py clears each week
        cur.execute("select key from public.reference_candidates"); self.assertEqual(cur.fetchall(), [("zebra sustainability standards board",)])
        cur.execute("select public.admin_reference_candidate_set('k', 'zebra sustainability standards board', true)"); self.assertTrue(cur.fetchone()[0]["ok"])
        cur.execute("select public.admin_reference_candidates('k')"); self.assertEqual(cur.fetchone()[0]["setters"], [])
        cur.execute("delete from public.reference_candidates"); cur.execute("delete from public.reference_watch")
        src = open(os.path.join(os.path.dirname(os.path.abspath(W.__file__)), "reference_candidates.py"), encoding="utf-8").read()
        self.assertIn("where status = 'found' and source = 'reports'", src)

    def test_a_term_gets_examples_from_the_reports(self):
        """23 Nov 2026: a term's card shows how reports write about it. The sentence is cut from the page with no model:
        prose that explains or applies the term, not a line of a contents page; one a company; a rejected one is gone."""
        import reference_examples as E
        good = ("Our approach. In 2024 we carried out a zebra materiality assessment in line with the applicable reporting standards, looking both at how such matters affect "
                "our business and at how our business affects people and the environment around it.")
        self.assertEqual(E.excerpt_of("Contents 1 Zebra materiality .... 12 Governance .... 14.", "Zebra Materiality"), None)
        score, text = E.excerpt_of("Contents. " + good + " Zebra materiality 4 5 6 7 2024 2023.", "Zebra Materiality"); self.assertGreaterEqual(score, 4); self.assertTrue(text.startswith("In 2024 we carried out"))
        rows = [("RX1", 3, "RXA", 2024, "Annual Report", good), ("RX2", 9, "RXA", 2023, "Annual Report", good), ("RX3", 1, "RXB", 2024, "x", "Zebra materiality 12.")]
        got = E.pick(rows, "Zebra Materiality"); self.assertEqual([(g[0], g[1], g[2]) for g in got], [("RX1", 3, "RXA")])       # one a company, the newer; the bare line is no example
        cur = self.conn.cursor()
        cur.execute("insert into public.companies (wba_id, name) values ('RXA', 'Example Maker') on conflict do nothing")
        cur.execute("insert into public.documents (library_id, wba_id, company, doc_type, year, label, url, pages, text_status) values ('RX1', 'RXA', 'Example Maker', 'Annual Report', 2024, 'a', 'https://x.example/a.pdf', 9, 'ok')")
        cur.executemany("insert into public.reference_examples (entry, library_id, page, wba_id, year, excerpt, score) values ('term:zm', %s, %s, %s, %s, %s, %s)", got)
        of = lambda: (cur.execute("select public.reference_examples_of('k', 'term:zm')"), cur.fetchone()[0])[1]
        self.assertEqual([(x["company"], x["year"], x["page"], x["url"], x["status"]) for x in of()], [("Example Maker", 2024, 3, "https://x.example/a.pdf", "found")])
        cur.execute("select public.admin_reference_example_set('k', 'term:zm', 'RX1', 3, 'rejected')"); self.assertTrue(cur.fetchone()[0]["ok"]); self.assertEqual(of(), [])
        cur.execute("select public.admin_reference_example_set('k', 'term:zm', 'RX9', 3, 'approved')"); self.assertFalse(cur.fetchone()[0]["ok"])
        cur.execute("delete from public.reference_examples where entry = 'term:zm'"); cur.execute("delete from public.documents where library_id = 'RX1'"); cur.execute("delete from public.companies where wba_id = 'RXA'")

    def test_the_words_of_a_year_and_those_that_rose(self):
        """23 Nov 2026: which words recur most in a year's reports. Pairs of content words, once a company a year, on
        pages in English; the owner sees a year's most used and what rose against the year before, in points."""
        import keywords_by_year as K
        eng = ("We are a company and the supply chain is important to our business in this year. The supply chain was reviewed and it is in line with "
               "our double materiality assessment for the year. ")
        self.assertEqual(K.pairs("The supply chain is important. Supply chain; and the Supply Chain!"), {"supply chain"})
        self.assertFalse(K.english("nous sommes une entreprise et la chaine est importante".split() * 10))
        rows = ([(f"W{i}", 2025, eng * 2) for i in range(10)] + [(f"W{i}", 2024, eng.replace("double materiality", "supply resilience") * 2) for i in range(8)]
                + [("X", 2025, "Nous sommes une entreprise et la chaîne est importante. " * 20), ("Y", None, eng * 2)])
        firms, got = K.count(rows, keep=3, min_companies=8)
        self.assertEqual(firms, {2025: 10, 2024: 8})
        self.assertIn((2025, "double materiality", 10), got); self.assertIn((2024, "supply chain", 8), got); self.assertNotIn("entreprise", " ".join(p for _, p, _ in got))
        cur = self.conn.cursor()
        cur.executemany("insert into public.keyword_use (year, phrase, companies, sampled) values (%s, %s, %s, %s)", [(y, p, n, firms[y]) for y, p, n in got])
        cur.execute("select public.admin_keyword_use('k', 2025, 'rising')"); r = cur.fetchone()[0]
        self.assertEqual((r["year"], r["sampled"], r["sampled_before"]), (2025, 10, 8))
        top = r["rows"][0]; self.assertEqual((top["phrase"] in ("double materiality", "materiality assessment"), top["before"], float(top["moved"])), (True, None, 100.0))
        cur.execute("select public.admin_keyword_use('k', 2025, 'most')"); most = {x["phrase"]: float(x["moved"]) for x in cur.fetchone()[0]["rows"]}; self.assertEqual(most["supply chain"], 0.0)
        cur.execute("delete from public.keyword_use")

    def test_a_word_of_the_search_names_a_group_by_what_companies_say_they_are(self):
        """23 Nov 2026: sub-sectors that are in no list. A word that several companies' profiles use for their sector,
        industry or sub-sector names a group, plural or singular; a word of every business does not, nor one of a single company."""
        cur = self.conn.cursor()
        rows = [("PG1", "subsector", "Zebra semiconductors"), ("PG2", "industry", "Semiconductor manufacturing"), ("PG3", "subsector", "Semiconductor equipment"), ("PG3", "sector", "Technology services"),
                ("PG4", "subsector", "Zebra airlines"), ("PG5", "sector", "Technology services"), ("PG6", "sector", "Technology services")]
        cur.executemany("insert into public.company_profile (wba_id, field, value, model) values (%s, %s, to_jsonb(%s::text), 'm')", rows)
        cur.execute("update public.company_profile set status = 'rejected' where wba_id = 'PG2'")
        ask = lambda q: (cur.execute("select public.profile_groups('k', %s)", (q,)), cur.fetchone()[0])[1]
        self.assertEqual(ask("semiconductors water use"), [])                                   # PG2's line was rejected: two companies are not a group
        cur.execute("update public.company_profile set status = 'read' where wba_id = 'PG2'")
        g = ask("Semiconductors water use"); self.assertEqual([(x["word"], x["companies"], sorted(x["ids"])) for x in g], [("semiconductors", 3, ["PG1", "PG2", "PG3"])])
        self.assertEqual(len(g[0]["says"]), 3)
        self.assertEqual(ask("airline services emissions"), [])                                 # one airline; "services" is a word of every business
        self.assertEqual([x["word"] for x in ask("technology companies")], ["technology"])
        cur.execute("delete from public.company_profile where wba_id like 'PG_'")

    def test_the_owner_sees_and_judges_a_company_profile(self):
        """23 Nov 2026: the profile read from a company's reports is shown to the owner first, field by field with
        where it was read; what the owner confirms, rejects or corrects is kept, and a correction carries a value."""
        import psycopg
        cur = self.conn.cursor()
        cur.execute("insert into public.companies (wba_id, name, industry) values ('PF1', 'Profiled One', 'Testing industry'), ('PF2', 'Other Two', null) on conflict do nothing")
        cur.execute("insert into public.company_profile (wba_id, field, value, library_id, page, model) values ('PF1', 'headquarters_city', '\"Lima\"', 'none', 4, 'm'), "
                    "('PF1', 'countries', '[\"Peru\", \"Chile\"]', null, null, 'm'), ('PF2', 'year_founded', '1950', null, null, 'm')")
        ask = lambda s=None: (cur.execute("select public.admin_company_profiles('k', %s)", (s,)), cur.fetchone()[0])[1]
        r = ask(); mine = [c for c in r["shown"] if c["id"] == "PF1"][0]
        self.assertGreaterEqual(r["companies"], 2); self.assertEqual([(f["field"], f["value"], f["page"], f["status"]) for f in mine["fields"]], [("countries", ["Peru", "Chile"], None, "read"), ("headquarters_city", "Lima", 4, "read")])
        self.assertEqual([c["id"] for c in ask("profiled")["shown"]], ["PF1"]); self.assertEqual(ask("no such name")["shown"], [])
        put = lambda *a: (cur.execute("select public.admin_company_profile_set('k', %s, %s, %s, %s::jsonb)", a), cur.fetchone()[0])[1]
        self.assertTrue(put("PF1", "headquarters_city", "confirmed", None)["ok"]); self.assertTrue(put("PF1", "countries", "corrected", '["Peru"]')["ok"]); self.assertFalse(put("PF1", "no_field", "rejected", None)["ok"])
        cur.execute("select field, value, status, model from public.company_profile where wba_id = 'PF1' order by field")
        self.assertEqual(cur.fetchall(), [("countries", ["Peru"], "corrected", "person"), ("headquarters_city", "Lima", "confirmed", "m")])
        for bad in (("PF1", "countries", "corrected", None), ("PF1", "countries", "corrected", "[]"), ("PF1", "countries", "liked", None)):
            with self.assertRaises(psycopg.Error): put(*bad)
        self.assertEqual(ask()["status"].get("corrected"), 1)
        # the page's profile: what was not rejected, each field with its page
        put("PF1", "headquarters_city", "rejected", None)
        cur.execute("select public.company_profile_of('k', 'PF1')"); card = cur.fetchone()[0]
        self.assertEqual({f: (x["value"], x["status"]) for f, x in card.items()}, {"countries": (["Peru"], "corrected")})
        cur.execute("select public.company_profile_of('k', 'no-such')"); self.assertIsNone(cur.fetchone()[0])
        cur.execute("delete from public.company_profile where wba_id in ('PF1', 'PF2')"); cur.execute("delete from public.companies where wba_id in ('PF1', 'PF2')")

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
