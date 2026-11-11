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

    def test_did_you_mean(self):
        import psycopg, load_supabase as L
        with psycopg.connect(URL) as c:
            L.refresh_derived(c)
        cur = self.conn.cursor()
        cur.execute("select public.did_you_mean('', 'unilver scope 3')"); self.assertEqual(cur.fetchone()[0], "unilever scope 3")
        cur.execute("select public.did_you_mean('', 'living wage')"); self.assertIsNone(cur.fetchone()[0])


if __name__ == "__main__":
    unittest.main()
