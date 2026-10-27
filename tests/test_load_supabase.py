import os
import tempfile
import unittest

import load_supabase as L
import text_core as tc

P1 = "20261001T000000-r1-00-001"
P2 = "20261020T000000-r1-00-001"


def store_files(folder):
    """A small store: doc a extracted twice (P2 wins, with fewer pages), b once, c failed."""
    tc.write_jsonl_gz(os.path.join(folder, f"pages-{P1}.jsonl.gz"),
                      [{"id": "a", "p": 1, "t": "old one"}, {"id": "a", "p": 2, "t": "old two"},
                       {"id": "a", "p": 3, "t": "old three"}, {"id": "b", "p": 1, "t": "net zero by 2050\x00"}])
    tc.write_jsonl_gz(os.path.join(folder, f"docs-{P1}.jsonl.gz"),
                      [{"id": "a", "rec": "recA", "status": "ok", "pages": 3, "chars": 21, "at": "2026-10-01"},
                       {"id": "b", "rec": "recB", "status": "ok", "pages": 1, "chars": 16, "at": "2026-10-01"}])
    tc.write_jsonl_gz(os.path.join(folder, f"pages-{P2}.jsonl.gz"),
                      [{"id": "a", "p": 1, "t": "new one"}, {"id": "a", "p": 2, "t": "new two"}])
    tc.write_jsonl_gz(os.path.join(folder, f"docs-{P2}.jsonl.gz"),
                      [{"id": "a", "rec": "recA", "status": "ok", "pages": 2, "chars": 14, "at": "2026-10-20"},
                       {"id": "c", "rec": "recC", "status": "no_text", "pages": 4, "chars": 0, "at": "2026-10-20"}])
    # an interrupted flush: pages without its docs file
    tc.write_jsonl_gz(os.path.join(folder, "pages-20261021T000000-r1-00-001.jsonl.gz"), [{"id": "z", "p": 1, "t": "x"}])
    tc.write_jsonl_gz(os.path.join(folder, "meta-20261020T000000.jsonl.gz"),
                      [{"id": "a", "rec": "recA", "wba": "WBA1", "co": "Acme", "type": "Annual Report", "year": 2025,
                        "lang": "en", "label": "Acme AR 2025", "url": "https://acme.com/ar.pdf", "pages": 2},
                       {"id": "b", "rec": "recB", "wba": "WBA2", "co": "Beta", "type": "Sustainability Report", "year": 2024}])
    tc.write_jsonl_gz(os.path.join(folder, "evidence-20261021T010000.jsonl.gz"),
                      [{"id": "b", "p": 1, "n": 0, "dims": ["climate"], "terms": {"climate": ["net zero"]},
                        "topics": {"climate": ["targets"]}, "area": "planning", "integration": [], "number": True,
                        "t": "net zero by 2050"},
                       {"id": "q", "p": 1, "n": 0, "dims": ["social"], "t": "not in the store"}])
    tc.write_jsonl_gz(os.path.join(folder, "evidence-docs-20261021T010000.jsonl.gz"),
                      [{"id": "b", "passages": 1, "v": "lex1"}, {"id": "q", "passages": 1, "v": "lex1"}])
    # an evidence run still uploading (no docs file yet)
    tc.write_jsonl_gz(os.path.join(folder, "evidence-20261022T010000.jsonl.gz"), [])


class Planning(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        store_files(self.dir)
        self.listing = tc.LocalStore(self.dir).assets()

    def test_plan_skips_incomplete_and_loaded_files(self):
        pages, ev = L.plan(self.listing, set())
        self.assertEqual(pages, [f"pages-{P1}.jsonl.gz", f"pages-{P2}.jsonl.gz"])
        self.assertEqual([e[0] for e in ev], ["20261021T010000"])
        pages, ev = L.plan(self.listing, {f"pages-{P1}.jsonl.gz", "evidence-20261021T010000.jsonl.gz"})
        self.assertEqual(pages, [f"pages-{P2}.jsonl.gz"])
        self.assertEqual(ev, [])

    def test_evidence_docs_file_is_not_taken_for_a_passages_file(self):
        self.assertEqual(L.evidence_files(["evidence-docs-20261021T010000.jsonl.gz"]), [])

    def test_only_the_newest_text_of_a_document_is_copied(self):
        docs, _ = tc.complete_parts(self.listing)
        index = tc.load_docs_index(self.dir, docs)
        old = list(L.current_pages(tc.read_jsonl_gz(os.path.join(self.dir, f"pages-{P1}.jsonl.gz")), index, P1))
        self.assertEqual(old, [("b", 1, "net zero by 2050")])     # a's old pages skipped, NUL removed
        new = list(L.current_pages(tc.read_jsonl_gz(os.path.join(self.dir, f"pages-{P2}.jsonl.gz")), index, P2))
        self.assertEqual([r[:2] for r in new], [("a", 1), ("a", 2)])

    def test_document_row(self):
        row = L.document_row({"id": "a", "rec": "recA", "status": "ok", "pages": 2, "chars": 14, "at": "2026-10-20",
                              "part": P2}, {"wba": "WBA1", "co": "Acme", "type": "Annual Report", "year": "2025"})
        self.assertEqual(row[:6], ("a", "recA", "WBA1", "Acme", "Annual Report", 2025))
        self.assertEqual(row[11:13], ("ok", P2))
        self.assertIsNone(L.document_row({"id": "c", "status": "no_text"}, {})[2])


@unittest.skipUnless(os.environ.get("PG_TEST_URL"), "set PG_TEST_URL to a throwaway Postgres to run")
class AgainstPostgres(unittest.TestCase):
    """End to end on a local Postgres without pgvector: the vector column becomes real[]."""

    def test_load_twice(self):
        import psycopg
        d = tempfile.mkdtemp()
        store_files(d)
        schema = open(L.SCHEMA).read()
        schema = "\n".join(l for l in schema.splitlines() if "extension if not exists vector" not in l)
        schema = schema.replace("extensions.vector(1024)", "real[]")
        L.SCHEMA = os.path.join(d, "schema.sql"); open(L.SCHEMA, "w").write(schema)
        url = os.environ["PG_TEST_URL"]
        with psycopg.connect(url) as c:
            c.execute("drop table if exists public.datapoints, public.passages, public.pages, public.documents, "
                      "public.load_log, public.load_state, public.search_access cascade")
            c.execute("drop function if exists public.search_pages, public.search_summary, public.search_passages")
            # the state of the database before this change: pages without stored words, the text-only index
            c.execute("create table public.documents (library_id text primary key, record_id text, wba_id text, company text, "
                      "doc_type text, year int, lang text, label text, url text, pages int, chars bigint, text_status text, "
                      "text_part text, extracted_at text, loaded_at timestamptz not null default now())")
            c.execute("create table public.pages (library_id text not null references public.documents (library_id), "
                      "page int not null, body text not null, primary key (library_id, page))")
            c.execute("create or replace function public.page_words(t text) returns tsvector language sql immutable "
                      "parallel safe as $$ select to_tsvector('simple'::regconfig, left(t, 200000)) $$")
            c.execute("create index pages_words on public.pages using gin (public.page_words(body))")
            c.execute("insert into public.documents (library_id, wba_id, company, year) values ('old', 'WBA9', 'Oldco', 2023)")
            c.execute("insert into public.pages (library_id, page, body) values ('old', 1, 'net zero target for 2040'), "
                      "('old', 2, 'water withdrawal fell')")
            # what Supabase provides and a plain Postgres lacks: the auth schema and the API roles
            c.execute("create schema if not exists auth")
            c.execute("create or replace function auth.jwt() returns jsonb language sql stable as "
                      "$$ select coalesce(nullif(current_setting('test.jwt', true), ''), '{}')::jsonb $$")
            for role in ("anon", "authenticated"):
                if not c.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone():
                    c.execute(f"create role {role}")
        os.environ.update({"SUPABASE_DB_URL": url, "TEXT_STORE_DIR": os.path.join(d, "store")})
        os.makedirs(os.environ["TEXT_STORE_DIR"])
        for n in os.listdir(d):
            if n.endswith(".gz"):
                os.rename(os.path.join(d, n), os.path.join(d, "store", n))
        import sys
        for _ in range(2):                                  # the second run must change nothing
            sys.argv = ["load_supabase.py", "--commit", "--index", "--max-gb", "50"]
            L.main()
        with psycopg.connect(url) as c:
            pages = c.execute("select library_id, page, body from public.pages order by 1, 2").fetchall()
            self.assertEqual(pages, [("a", 1, "new one"), ("a", 2, "new two"), ("b", 1, "net zero by 2050"),
                                     ("old", 1, "net zero target for 2040"), ("old", 2, "water withdrawal fell")])
            # the migration: older rows backfilled, new rows filled by the trigger, one index, old one gone
            self.assertEqual(c.execute("select count(*) from public.pages where words is null or year is null "
                                       "or doc_type is null or wba_id is null").fetchone()[0], 0)
            self.assertEqual(c.execute("select year, doc_type, wba_id from public.pages where library_id = 'old' and page = 1").fetchone(),
                             (2023, "", "WBA9"))
            self.assertEqual(c.execute("select year, doc_type, wba_id from public.pages where library_id = 'a' and page = 1").fetchone(),
                             (2025, "Annual Report", "WBA1"))
            self.assertEqual(c.execute("select value from public.load_state where key = 'words_backfill'").fetchone(), ("done",))
            self.assertEqual(c.execute("select to_regclass('public.pages_search') is not null, to_regclass('public.pages_words') is null").fetchone(),
                             (True, True))
            # a correction in the library (year, type, company) reaches the pages through the trigger
            c.execute("update public.documents set year = 2026, doc_type = 'Integrated Report' where library_id = 'old'")
            self.assertEqual(c.execute("select distinct year, doc_type from public.pages where library_id = 'old'").fetchall(),
                             [(2026, "Integrated Report")])
            c.execute("update public.documents set year = 2023, doc_type = null where library_id = 'old'")
            self.assertEqual(c.execute("select distinct year, doc_type from public.pages where library_id = 'old'").fetchall(),
                             [(2023, "")])
            self.assertEqual(c.execute("select count(*) from public.documents").fetchone()[0], 4)
            self.assertEqual(c.execute("select company, year from public.documents where library_id='a'").fetchone(),
                             ("Acme", 2025))
            self.assertEqual(c.execute("select library_id, dims, lexicon from public.passages").fetchall(),
                             [("b", ["climate"], "lex1")])
            hit = c.execute("select library_id from public.pages where words @@ plainto_tsquery('simple', 'net zero') order by 1").fetchall()
            self.assertEqual(hit, [("b",), ("old",)])
            self.assertEqual(c.execute("select count(*) from public.load_log").fetchone()[0], 3)
            # search functions (search.sql): a direct session may search; the page's users go through can_search()
            # newest documents first (b is 2024, old is 2023), then by company within a year
            rows = c.execute("select library_id, page, year, excerpt from public.search_pages('\"net zero\"')").fetchall()
            self.assertEqual([(r[0], r[1], r[2]) for r in rows], [("b", 1, 2024), ("old", 1, 2023)])
            self.assertIn("⟦net⟧", rows[0][3])
            self.assertEqual([r[0] for r in c.execute("select library_id from public.search_pages('\"net zero\"', max_rows => 1)").fetchall()], ["b"])
            # filters: year, type, company by name or id; an unknown company finds nothing
            self.assertEqual([r[0] for r in c.execute("select library_id from public.search_pages('\"net zero\"', year_from => 2024)").fetchall()], ["b"])
            self.assertEqual([r[0] for r in c.execute("select library_id from public.search_pages('one', type_filter => 'Annual Report')").fetchall()], ["a"])
            self.assertEqual([r[0] for r in c.execute("select library_id from public.search_pages('\"net zero\"', company_filter => 'oldco')").fetchall()], ["old"])
            self.assertEqual([r[0] for r in c.execute("select library_id from public.search_pages('\"net zero\"', company_filter => 'WBA2')").fetchall()], ["b"])
            self.assertEqual(c.execute("select count(*) from public.search_pages('\"net zero\"', company_filter => 'nobody')").fetchone()[0], 0)
            # several companies and types at once, separated by ";"
            self.assertEqual([r[0] for r in c.execute("select library_id from public.search_pages('\"net zero\"', company_filter => 'oldco; WBA2')").fetchall()], ["b", "old"])
            self.assertEqual([r[0] for r in c.execute("select library_id from public.search_pages('one', type_filter => 'Annual Report; CSR Report')").fetchall()], ["a"])
            self.assertEqual(c.execute("select count(*) from public.search_pages('one', type_filter => 'CSR Report')").fetchone()[0], 0)
            # the pages of one document, in page order
            self.assertEqual([(r[0], r[1]) for r in c.execute("select library_id, page from public.search_pages('new', doc_filter => 'a')").fetchall()], [("a", 1), ("a", 2)])
            # one line per document, with its matching pages counted and its best page
            docs = c.execute("select library_id, pages, pages_capped, best_page, excerpt from public.search_documents('new')").fetchall()
            self.assertEqual([(d[0], d[1], d[2]) for d in docs], [("a", 2, False)])
            self.assertIn("⟦new⟧", docs[0][4])
            self.assertEqual([d[0] for d in c.execute("select library_id from public.search_documents('\"net zero\"')").fetchall()], ["b", "old"])
            self.assertEqual(c.execute("select count(*) from public.search_documents('\"net zero\"', year_to => 2023)").fetchone()[0], 1)
            self.assertEqual(c.execute("select companies, documents, pages, capped from public.search_summary('one')").fetchone(),
                             (1, 1, 1, False))                        # "new one": doc a
            self.assertEqual(c.execute("select companies, documents, pages from public.search_summary('\"net zero\"')").fetchone(), (2, 2, 2))
            self.assertEqual(c.execute("select pages from public.search_summary('\"net zero\"', year_to => 2023)").fetchone(), (1,))
            # the exact company count, probed company by company, with the same filters
            self.assertEqual(c.execute("select public.search_company_count('\"net zero\"')").fetchone()[0], 2)
            self.assertEqual(c.execute("select public.search_company_count('\"net zero\"', year_to => 2023)").fetchone()[0], 1)
            self.assertEqual(c.execute("select public.search_company_count('\"net zero\"', company_filter => 'oldco')").fetchone()[0], 1)
            self.assertEqual(c.execute("select public.search_company_count('\"net zero\"', company_filter => 'nobody')").fetchone()[0], 0)
            self.assertEqual(c.execute("select public.search_company_count('')").fetchone()[0], 0)
            self.assertEqual(c.execute("select library_id, dims from public.search_passages('zero', 'climate')").fetchall(),
                             [("b", ["climate"])])
            self.assertEqual(c.execute("select count(*) from public.search_pages('')").fetchone()[0], 0)
            # a document with no company (out of the library's scope now) is left out of searches
            c.execute("update public.documents set company = null, wba_id = null where library_id = 'b'")
            self.assertEqual([r[0] for r in c.execute("select library_id from public.search_pages('zero')").fetchall()], ["old"])
            self.assertEqual(c.execute("select pages from public.search_summary('zero')").fetchone(), (1,))
            c.execute("update public.documents set company = 'Beta', wba_id = 'WBA2' where library_id = 'b'")
            # who may search: a WBA address, or one listed in search_access
            may = lambda e: c.execute("select public.email_may_search(%s)", (e,)).fetchone()[0]
            self.assertTrue(may("Someone@WorldBenchmarkingAlliance.org"))
            self.assertFalse(may("someone@example.com"))
            self.assertFalse(may(None))
            c.execute("insert into public.search_access (email) values ('someone@example.com')")
            self.assertTrue(may("SOMEONE@example.com"))



if __name__ == "__main__":
    unittest.main()
