"""A real Postgres for the search functions' tests: the schema, the search functions and a small library.

The Tests workflow starts a Postgres service and sets PG_SEARCH_TEST_URL; locally the tests that need it are skipped
when it is not set. Everything is created in the database named by the URL and nothing outside it is touched.
The vector extension (the semantic index) is used when the server has it and left out when it does not: the
search functions do not need it.
"""
import os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

ROLES = """
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon; end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated; end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then create role service_role; end if;
end $$;
create schema if not exists extensions;
create schema if not exists auth;
create or replace function auth.jwt() returns jsonb language sql as $$ select '{"email":"tester@worldbenchmarkingalliance.org"}'::jsonb $$;
create extension if not exists pg_trgm with schema extensions;
create extension if not exists btree_gin with schema extensions;
create extension if not exists fuzzystrmatch with schema extensions;
"""


def table_ddl():
    """The create-table statements kept inside the loaders (companies, scores): the search functions read them."""
    out = []
    for name in ("load_companies.py", "load_scores.py"):
        src = open(os.path.join(ROOT, name), encoding="utf-8").read()
        for m in re.finditer(r'"""(.*?)"""', src, re.S):
            if "create table if not exists public." in m.group(1):
                out.append(m.group(1).rstrip().rstrip(";") + ";")
    return "\n".join(out)


def install(conn):
    """Roles, schema.sql, the loaders' tables, the page index and search.sql (as the loader installs it)."""
    import load_supabase as L
    cur = conn.cursor()
    cur.execute(ROLES)
    with open(os.path.join(ROOT, "supabase", "schema.sql"), encoding="utf-8") as fh:
        schema = fh.read()
    cur.execute("select 1 from pg_available_extensions where name = 'vector'")
    if not cur.fetchone():
        schema = "\n".join(l for l in schema.split("\n") if "create extension if not exists vector" not in l and "using hnsw" not in l)
        schema = re.sub(r"extensions\.vector\(\d+\)", "real[]", schema)
    cur.execute(schema)
    cur.execute(table_ddl())
    cur.execute("set search_path = public, extensions")
    cur.execute("create index if not exists pages_search on public.pages using gin (words, year, doc_type, wba_id)")
    cur.execute(L.guard_sql(open(os.path.join(ROOT, "supabase", "search.sql"), encoding="utf-8").read()))
    conn.commit()


FILLER = ("the company reports on climate water energy people suppliers workers wage living just transition plan human rights "
          "due diligence policy board risk growth revenue scope emissions target intensity lifecycle sold products categories "
          "methodological change unique reporting several including").split()


def seed(conn, docs=120, pages=12):
    """A small library: `docs` documents of `pages` pages of filler words, a few with known sentences, the recent
    main reports with their word pairs. Deterministic."""
    import random
    from semantic_index import pair_lexemes, pairs_literal
    rnd = random.Random(1)
    cur = conn.cursor()
    cur.execute("truncate public.page_pairs, public.pages, public.documents cascade")
    cur.execute("delete from public.companies")
    for i in range(40):
        cur.execute("insert into public.companies (wba_id, name) values (%s, %s)", (f"PT_{i:05d}", "Unilever" if i == 0 else "Target" if i == 1 else f"Company {i}"))
    for d in range(docs):
        lid, year = f"doc{d}", rnd.choice([2021, 2022, 2023, 2024, 2025, 2026])
        typ, wba = rnd.choice(["Sustainability Report", "Annual Report", "Code of Conduct"]), f"PT_{d % 40:05d}"
        cur.execute("insert into public.documents (library_id, wba_id, company, doc_type, year, label, url, pages) values (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (lid, wba, f"Company {d % 40}", typ, year, f"{typ} {year} number {d}", f"https://example.org/{lid}.pdf", pages))
        for pg in range(1, pages + 1):
            body = " ".join(rnd.choice(FILLER) for _ in range(120))
            if d % 7 == 0 and pg == 3:
                body += " we pay a living wage to all workers"
            if d % 13 == 0 and pg == 7:
                body += " human rights due diligence is carried out yearly"
            cur.execute("insert into public.pages (library_id, page, body, words, year, doc_type, wba_id) values (%s,%s,%s,to_tsvector('simple', %s),%s,%s,%s)",
                        (lid, pg, body, body, year, typ, wba))
            if year >= 2024 and typ != "Code of Conduct":
                cur.execute("insert into public.page_pairs (library_id, page, pairs) values (%s,%s,%s::tsvector)", (lid, pg, pairs_literal(pair_lexemes(body))))
    # two pages of one company for the ranking case of 11 Nov 2026: a table of Scope 3 emissions, and the target
    for lid, body in (("tablepage", "zebra scope 3 emissions 47.21 56.61 scope 3 emissions purchased goods scope 3 emissions indirect use scope 3 emissions total"),
                      ("targetpage", " ".join(["filler words about other things in the annual report"] * 30)
                       + " zebra we have set a scope 3 emissions target to reduce value chain emissions by 42 percent by 2030 "
                       + " ".join(["more filler text follows here"] * 20))):
        cur.execute("insert into public.documents (library_id, wba_id, company, doc_type, year, label, url, pages) values (%s,'PT_00000','Unilever','Annual Report',2026,%s,%s,1)",
                    (lid, f"Annual Report 2026 {lid}", f"https://example.org/{lid}.pdf"))
        cur.execute("insert into public.pages (library_id, page, body, words, year, doc_type, wba_id) values (%s,1,%s,to_tsvector('simple', %s),2026,'Annual Report','PT_00000')", (lid, body, body))
    cur.execute("analyze")
    conn.commit()
