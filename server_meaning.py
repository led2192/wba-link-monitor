#!/usr/bin/env python3
"""
server_meaning.py: the passages and their index on the server's own database, and the like for like comparison
with Supabase. Step 4 of the move of the search by meaning (docs/server.md, "The database").

Two things, each on its own:

  --load      copies every passage (its key and its half-precision embedding; the text stays in Supabase) and, for
              each document with passages, its company, year and type, from Supabase into the schema "meaning" of the
              server's database; compares the counts a document on both sides; builds the index once, as Supabase
              defines its own. Needs --commit. Refuses when the schema already holds passages, unless --reload,
              which empties the server's copy first (the server's copy only: Supabase is only ever read).
  --compare N asks N kept question vectors both ways, each four times (no filter, two years, a type, a company), with
              the real search on Supabase (find_meaning) and the same search written for the server, and reports the
              marks of stage A: of Supabase's first ten pages, the share in the server's first ten (mark: 98%), and
              the time of a search on the server (marks: median under 300 ms, 95th percentile under 800 ms). It also
              counts the searches with a year or a type where Supabase returns fewer than ten pages because it filters
              after taking the nearest 400 passages, and what the server returns when it filters while it searches.
              Reads only, on both sides.

  --sample P  with --load: a block sample of P percent into the schema "meaning_trial" instead, to try the whole
              path small first. --compare then reads that schema with --trial (its overlap means nothing: most
              passages are missing; it shows that the path runs).

Without --commit, --load says what it would do and what both sides hold.

Prints counts, shares and times only: no question, document, page or text.

Env: SUPABASE_DB_URL (read), LOCAL_DB_URL (the server's database; a role that owns or may create the schema).
"""
import argparse, math, os, re, statistics, sys, time

import qa

DIMENSIONS = 384
EF_SEARCH = 200                 # as find_meaning sets it (search.sql)
NEAR = 400                      # the nearest passages taken before pages are chosen, as find_meaning does
PER_DOC = 3                     # pages a document may give
MARK_OVERLAP, MARK_MEDIAN_MS, MARK_P95_MS = 0.98, 300, 800
VARIANTS = ("no filter", "two years", "a type", "a company")
YEARS = (2024, 2025)
TYPE = "Sustainability Report"


def percentile(values, p):
    v = sorted(values)
    return v[min(len(v) - 1, max(0, math.ceil(p / 100 * len(v)) - 1))] if v else 0


def overlap(theirs, ours):
    """Of the pages in each of Supabase's lists, the share present in the server's list for the same search."""
    want = sum(len(t) for t in theirs)
    return sum(len(set(t) & set(o)) for t, o in zip(theirs, ours)) / want if want else 1.0


def conditions(alias="f"):
    return (f"(%(y0)s::int is null or {alias}.year >= %(y0)s) and (%(y1)s::int is null or {alias}.year <= %(y1)s) "
            f"and (%(types)s::text[] is null or {alias}.doc_type = any(%(types)s))")


def server_sql(schema, company, inside=False):
    """The search of find_meaning written for the server's tables. company: the exact search inside the company's
    documents (as Supabase does); else the index, the nearest NEAR passages, and the year and type applied after
    (as Supabase does) or, with inside=True, while the index is walked (pgvector's iterative scan)."""
    q = f"%(q)s::halfvec({DIMENSIONS})"
    tail = (f" pages as (select distinct on (n.library_id, n.page) n.library_id, n.page, n.dist from near n order by n.library_id, n.page, n.dist), "
            f"kept as (select p.*, row_number() over (partition by p.library_id order by p.dist) as in_doc from pages p{{join}}) "
            f"select x.library_id || '|' || x.page from kept x where x.in_doc <= {PER_DOC} order by x.dist limit 10")
    if company:
        return (f"with docs as (select f.library_id from {schema}.chunk_filters f where f.wba_id = any(%(ids)s) and {conditions()}), "
                f"near as materialized (select c.library_id, c.page, (c.embedding <=> {q}) + 0 as dist from {schema}.chunks c "
                f"where c.library_id in (select library_id from docs) order by dist limit {NEAR}),"            # + 0: an exact search, never the index
                + tail.format(join=""))
    if inside:
        return (f"with near as materialized (select c.library_id, c.page, (c.embedding <=> {q}) as dist from {schema}.chunks c "
                f"join {schema}.chunk_filters f on f.library_id = c.library_id where {conditions()} order by c.embedding <=> {q} limit {NEAR}),"
                + tail.format(join=""))
    return (f"with near as materialized (select c.library_id, c.page, (c.embedding <=> {q}) as dist from {schema}.chunks c "
            f"order by c.embedding <=> {q} limit {NEAR}),"
            + tail.format(join=f" join {schema}.chunk_filters f on f.library_id = p.library_id where {conditions()}"))


def load(src, dst, schema, sample, reload, memory, workers, out=print):
    """Copy, compare counts, build the index. Returns a dict of what was measured."""
    s, d = src.cursor(), dst.cursor()
    d.execute(f"create schema if not exists {schema}")
    d.execute(f"create table if not exists {schema}.chunks (library_id text not null, page int not null, n int not null, embedding halfvec({DIMENSIONS}) not null)")
    d.execute(f"create table if not exists {schema}.chunk_filters (library_id text primary key, wba_id text, year int, doc_type text, live boolean not null default true)")
    d.execute(f"select count(*) from {schema}.chunks")
    held = d.fetchone()[0]
    if held and not reload:
        dst.rollback()
        raise SystemExit(f"The schema {schema} already holds {held:,} passages. Nothing done. --reload empties the server's copy and loads again.")
    if held:
        d.execute(f"drop index if exists {schema}.chunks_embedding")
        d.execute(f"truncate {schema}.chunks, {schema}.chunk_filters")       # the server's copy; Supabase keeps the passages
    d.execute(f"alter table {schema}.chunks drop constraint if exists chunks_pkey")
    dst.commit()
    s.execute("set statement_timeout = '90min'")
    part = f" tablesample system ({float(sample)})" if sample else ""
    t = time.time()
    with s.copy(f"copy (select library_id, page, n, embedding::extensions.halfvec({DIMENSIONS}) from public.chunks{part} where embedding is not null) to stdout") as source, \
         d.copy(f"copy {schema}.chunks from stdin") as target:
        for block in source:
            target.write(block)
    dst.commit()
    copy_s = time.time() - t
    d.execute(f"select count(*), count(distinct library_id) from {schema}.chunks")
    rows, docs = d.fetchone()
    out(f">>> copied {rows:,} passages of {docs:,} documents in {copy_s / 60:.1f} min", flush=True)
    with s.copy("copy (select d.library_id, d.wba_id, d.year, d.doc_type from public.documents d where exists (select 1 from public.chunk_docs c where c.library_id = d.library_id)) to stdout") as source, \
         d.copy(f"copy {schema}.chunk_filters (library_id, wba_id, year, doc_type) from stdin") as target:
        for block in source:
            target.write(block)
    dst.commit()
    d.execute(f"select count(*) from {schema}.chunk_filters")
    filters = d.fetchone()[0]
    d.execute(f"select count(distinct c.library_id) from {schema}.chunks c where not exists (select 1 from {schema}.chunk_filters f where f.library_id = c.library_id)")
    orphans = d.fetchone()[0]
    differ = None
    if not sample:                                   # the counts a document, on both sides
        s.execute("select library_id, count(*) from public.chunks where embedding is not null group by 1")
        theirs = dict(s.fetchall())
        d.execute(f"select library_id, count(*) from {schema}.chunks group by 1")
        ours = dict(d.fetchall())
        differ = sum(1 for k in set(theirs) | set(ours) if theirs.get(k) != ours.get(k))
    src.rollback()
    d.execute(f"alter table {schema}.chunks add constraint chunks_pkey primary key (library_id, page, n)")
    d.execute(f"set maintenance_work_mem = '{memory}'")
    d.execute(f"set max_parallel_maintenance_workers = {int(workers)}")
    t = time.time()
    d.execute(f"create index chunks_embedding on {schema}.chunks using hnsw (embedding halfvec_cosine_ops) with (m = 16, ef_construction = 64)")
    d.execute(f"analyze {schema}.chunks")
    d.execute(f"analyze {schema}.chunk_filters")
    dst.commit()
    build_s = time.time() - t
    d.execute(f"select pg_table_size('{schema}.chunks'), pg_relation_size('{schema}.chunks_embedding')")
    table_bytes, index_bytes = d.fetchone()
    return dict(rows=rows, docs=docs, filters=filters, orphans=orphans, differ=differ, copy_s=copy_s, build_s=build_s, table_bytes=table_bytes, index_bytes=index_bytes)


def load_report(m, schema, memory, workers):
    gb = lambda b: b / 1024 ** 3
    return [f"### The passages on the server (schema {schema})", "", "| Measured | Value |", "|---|---|",
            f"| Passages copied | {m['rows']:,} of {m['docs']:,} documents in {m['copy_s'] / 60:.1f} min |",
            f"| Documents with their company, year and type | {m['filters']:,}; documents with passages and no such line: {m['orphans']:,} |",
            f"| Documents whose count of passages differs between Supabase and the server | {'not compared (a sample)' if m['differ'] is None else format(m['differ'], ',')} |",
            f"| Table | {gb(m['table_bytes']):.2f} GB |",
            f"| Index | {gb(m['index_bytes']):.2f} GB, built in {m['build_s'] / 60:.1f} min with {memory} and {workers} workers |"]


def compare(src, dst, schema, n, out=print):
    """Stage A. Returns {variant: {theirs, ours, ms_server, ms_supabase, short, filled}} over n kept question vectors."""
    s, d = src.cursor(), dst.cursor()
    s.execute("select vec from public.query_vectors where array_length(vec, 1) = %s order by at desc limit %s", (DIMENSIONS, n))
    vectors = [r[0] for r in s.fetchall()]
    s.execute("select public.types_matching(%s)", (TYPE,))
    types = s.fetchone()[0]
    d.execute(f"set hnsw.ef_search = {EF_SEARCH}")
    res = {v: dict(theirs=[], ours=[], ms_server=[], ms_supabase=[], short=0, filled=0) for v in VARIANTS}
    for vec in vectors:
        qtext = "[" + ",".join(repr(float(x)) for x in vec) + "]"
        company = None
        for variant in VARIANTS:
            y0, y1 = YEARS if variant == "two years" else (None, None)
            tf = TYPE if variant == "a type" else None
            if variant == "a company" and not company:
                continue                                      # the search without filters found nothing to name a company from
            try:
                t = time.time()
                s.execute("select library_id || '|' || page, wba_id from public.find_meaning('eval', %s::real[], %s, %s, %s, %s, 10)",
                          (vec, company if variant == "a company" else None, y0, y1, tf))
                got = s.fetchall()
                ms_theirs = (time.time() - t) * 1000
                src.rollback()
                ids = None
                if variant == "a company":
                    s.execute("select public.companies_matching(%s)", (company,))
                    ids = s.fetchone()[0]
                    src.rollback()
                args = dict(q=qtext, y0=y0, y1=y1, types=types if tf else None, ids=ids)
                t = time.time()
                d.execute(server_sql(schema, variant == "a company"), args)
                ours = [r[0] for r in d.fetchall()]
                ms_ours = (time.time() - t) * 1000
                r = res[variant]
                r["theirs"].append([g[0] for g in got]); r["ours"].append(ours); r["ms_server"].append(ms_ours); r["ms_supabase"].append(ms_theirs)
                if variant == "no filter" and got:
                    company = got[0][1]
                if variant in ("two years", "a type") and len(got) < 10:
                    r["short"] += 1
                    d.execute("set hnsw.iterative_scan = relaxed_order")
                    d.execute(server_sql(schema, False, inside=True), args)
                    r["filled"] += 1 if len(d.fetchall()) >= 10 else 0
                    d.execute("set hnsw.iterative_scan = off")
                dst.rollback()
                d.execute(f"set hnsw.ef_search = {EF_SEARCH}")
            except Exception as e:
                src.rollback(); dst.rollback()
                d.execute(f"set hnsw.ef_search = {EF_SEARCH}")
                out(f"  one search not compared ({variant}: {type(e).__name__})")
    return res


def compare_report(res, schema):
    md = [f"### Stage A: the same searches on Supabase and on the server (schema {schema})", "",
          "| Search | Searches | Supabase's first ten found in the server's first ten | Same ten, same order | Server, ms (median / 95th) | Supabase seen from the server, ms (median / 95th) |",
          "|---|---|---|---|---|---|"]
    every_t, every_o, every_ms = [], [], []
    for v in VARIANTS:
        r = res[v]
        if not r["theirs"]:
            md.append(f"| {v} | 0 | | | | |"); continue
        same = sum(1 for t, o in zip(r["theirs"], r["ours"]) if t == o)
        md.append(f"| {v} | {len(r['theirs'])} | {100 * overlap(r['theirs'], r['ours']):.1f}% | {same} | {statistics.median(r['ms_server']):.0f} / {percentile(r['ms_server'], 95):.0f} | "
                  f"{statistics.median(r['ms_supabase']):.0f} / {percentile(r['ms_supabase'], 95):.0f} |")
        every_t += r["theirs"]; every_o += r["ours"]; every_ms += r["ms_server"]
    ov = overlap(every_t, every_o)
    med, p95 = (statistics.median(every_ms), percentile(every_ms, 95)) if every_ms else (0, 0)
    ok = lambda b: "PASS" if b else "FAIL"
    md += ["", f"Marks of stage A over {len(every_t)} searches: first ten {100 * ov:.1f}% (mark {100 * MARK_OVERLAP:.0f}%): {ok(ov >= MARK_OVERLAP)}; "
               f"median {med:.0f} ms (mark under {MARK_MEDIAN_MS}): {ok(med < MARK_MEDIAN_MS)}; 95th percentile {p95:.0f} ms (mark under {MARK_P95_MS}): {ok(p95 < MARK_P95_MS)}. "
               "The server's times are measured on the server; the hop from Supabase (6 ms measured on 15 Nov) is added once the service exists.",
           "", f"Searches with a year or a type where Supabase returns fewer than ten pages (it filters after taking the nearest {NEAR} passages): "
               f"{sum(res[v]['short'] for v in ('two years', 'a type'))}; of those, the server returns ten when it filters while it searches: {sum(res[v]['filled'] for v in ('two years', 'a type'))}."]
    return md, ov, med, p95


def main(argv=None):
    ap = argparse.ArgumentParser(description="The passages on the server's database and the comparison with Supabase.")
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--compare", type=int, default=0, help="kept question vectors to ask both ways")
    ap.add_argument("--sample", type=float, default=0, help="with --load: a block sample of this percent, into the schema meaning_trial")
    ap.add_argument("--trial", action="store_true", help="with --compare: read the schema meaning_trial")
    ap.add_argument("--reload", action="store_true", help="with --load: empty the server's copy first")
    ap.add_argument("--memory", default="8GB")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"\d{1,3}(MB|GB)", a.memory) or not 1 <= a.workers <= 12 or not 0 <= a.sample <= 100 or not 0 <= a.compare <= 2000:
        sys.exit("--memory like 8GB; --workers 1 to 12; --sample 0 to 100; --compare 0 to 2,000.")
    if not os.environ.get("SUPABASE_DB_URL") or not os.environ.get("LOCAL_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL and LOCAL_DB_URL.")
    schema = "meaning_trial" if (a.sample or a.trial) else "meaning"
    import psycopg
    run = qa.Run("server_meaning.py")
    md, counters = [], {}
    try:
        with psycopg.connect(os.environ["SUPABASE_DB_URL"]) as src, psycopg.connect(os.environ["LOCAL_DB_URL"]) as dst:
            s, d = src.cursor(), dst.cursor()
            s.execute("select reltuples::bigint from pg_class where oid = 'public.chunks'::regclass")
            theirs = s.fetchone()[0]
            d.execute("select to_regclass(%s) is not null", (f"{schema}.chunks",))
            ours = None
            if d.fetchone()[0]:
                d.execute(f"select count(*) from {schema}.chunks")
                ours = d.fetchone()[0]
            src.rollback(); dst.rollback()
            print(f">>> Supabase holds about {theirs:,} passages; the server's schema {schema} holds {'none (not created)' if ours is None else format(ours, ',')}", flush=True)
            if a.load and not a.commit:
                print(f">>> dry run: would copy {'a ' + str(a.sample) + '% sample of ' if a.sample else 'all '}the passages into {schema}"
                      f"{', emptying it first' if a.reload else ''}, compare the counts a document, and build the index with {a.memory} and {a.workers} workers. Nothing done. --commit runs it.")
            elif a.load:
                m = load(src, dst, schema, a.sample, a.reload, a.memory, a.workers)
                md += load_report(m, schema, a.memory, a.workers)
                counters.update(rows=m["rows"], docs=m["docs"], differ=m["differ"] if m["differ"] is not None else -1, copy_s=round(m["copy_s"]), build_s=round(m["build_s"]))
            if a.compare:
                res = compare(src, dst, schema, a.compare)
                lines, ov, med, p95 = compare_report(res, schema)
                md += ([""] if md else []) + lines
                counters.update(overlap_pct=round(100 * ov, 1), median_ms=round(med), p95_ms=round(p95))
        text = "\n".join(md)
        if text:
            print(text)
            if os.environ.get("GITHUB_STEP_SUMMARY"):
                open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8").write(text + "\n")
    except SystemExit:
        run.finish(status="ok", rows_in=0, rows_out=0)
        raise
    except Exception as e:
        run.failure("RUN_FAILED", f"server_meaning.py stopped: {type(e).__name__}", key="server_meaning", message=str(e)[:300])
        run.finish(status="failed")
        raise
    run.finish(status="ok", rows_in=counters.get("rows", 0), rows_out=counters.get("rows", 0), counters=counters)
    return 0


if __name__ == "__main__":
    sys.exit(main())
