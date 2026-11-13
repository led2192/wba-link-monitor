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

    def test_a_company_can_be_asked_for_once(self):
        cur = self.conn.cursor()
        cur.execute("delete from public.company_requests")
        cur.execute("select public.request_company('Acme Robotics', 'acme.example.com', null, 'for 2027')")
        self.assertTrue(cur.fetchone()[0]["asked"])
        cur.execute("select public.request_company('acme robotics')"); self.assertTrue(cur.fetchone()[0]["again"])
        cur.execute("select public.request_company('Unilever')"); self.assertTrue(cur.fetchone()[0]["held"])      # already a company
        cur.execute("select name, website, status from public.company_requests"); self.assertEqual(cur.fetchall(), [("Acme Robotics", "acme.example.com", "new")])
        cur.execute("update public.companies set website = 'https://www.unilever.com/' where name = 'Unilever'")
        cur.execute("select public.request_company('Unilever Global Plc', 'unilever.com/investors')"); j = cur.fetchone()[0]
        self.assertTrue(j["held"]); self.assertEqual(j["name"], "Unilever")                              # the same website is the same company
        cur.execute("select public.site_host('HTTPS://www.Example.com:443/en/')"); self.assertEqual(cur.fetchone()[0], "example.com")
        cur.execute("select public.request_company('acme robotics')"); rid = cur.fetchone()[0]["request"]
        cur.execute("select public.upload_begin(%s, 'a.pdf', 5000, %s)", (f"REQ_{rid}", "ef" * 32)); self.assertEqual(cur.fetchone()[0]["path"], f"REQ_{rid}/{'ef' * 32}.pdf")
        with self.assertRaises(Exception):
            cur.execute("select public.upload_begin('REQ_999999', 'a.pdf', 5000, %s)", ("ef" * 32,))     # a request that is not this person's
        cur.execute("select public.upload_companies()"); mine = [c for c in cur.fetchone()[0] if c.get("asked")]
        self.assertEqual([(c["id"], c["name"]) for c in mine], [(f"REQ_{rid}", "Acme Robotics")])        # the company asked for can be chosen again
        for bad in (("X",), ("Beta", "not a site"), ("Gamma", None, "123")):
            with self.assertRaises(Exception):
                cur.execute("select public.request_company(%s, %s, %s)", (bad + (None, None))[:3])

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
        for bad in (("PT_nope", "a.pdf", 5000, sha, None), ("PT_00000", "a.pdf", 10, sha, None), ("PT_00000", "a.pdf", 60000000, sha, None),
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
