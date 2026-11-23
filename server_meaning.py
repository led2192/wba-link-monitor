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
              Reads only, on both sides. --exact M adds, for M of them, the exact search (every passage read, no
              index) on the server, and says how much of that truth each index returns: the first run (16 Nov 2026)
              gave 93% agreement between the two indexes where no company is named, which fails the 98% mark and says
              nothing about which index is right.

  --refresh   keeps the server's copy current (the daily job; needs --commit): documents whose passages Supabase holds
              and the server does not, or holds in a different number, are copied again; documents Supabase no longer
              holds are marked not live (the service leaves them out; their passages stay); every document's company,
              year and type are brought up to date. Then the count a document is compared again: if any still
              differs, the option meaning_remote is switched off in Supabase (its own search carries on) and the run
              fails loudly. That one row is the only thing this script ever writes in Supabase.
  --switch N  asks N kept question vectors of Supabase's own search (find_meaning) with the option off and on, each
              with no filter, with two years and with a type (never a company: that path is exact on both sides),
              and judges both against the exact search on the server with the same filter. The comparison outside
              review 3 asked for before the switch. Reads only.

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


def exact_sql(schema):
    """The true nearest passages, by reading every passage (no index), then pages as find_meaning chooses them."""
    q = f"%(q)s::halfvec({DIMENSIONS})"
    return (f"with near as materialized (select c.library_id, c.page, (c.embedding <=> {q}) + 0 as dist from {schema}.chunks c order by dist limit {NEAR}), "
            f"pages as (select distinct on (n.library_id, n.page) n.library_id, n.page, n.dist from near n order by n.library_id, n.page, n.dist), "
            f"kept as (select p.*, row_number() over (partition by p.library_id order by p.dist) as in_doc from pages p) "
            f"select x.library_id || '|' || x.page from kept x where x.in_doc <= {PER_DOC} order by x.dist limit 10")


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


SAFE_ID = re.compile(r"^[A-Za-z0-9_.:\-]{1,80}$")


def drift(theirs, ours, dead=()):
    """(new, changed, gone): documents Supabase has and the server lacks; both have, in different numbers; the server
    has and Supabase lacks (those already marked not live are not counted as gone again)."""
    new = sorted(k for k in theirs if k not in ours)
    changed = sorted(k for k in theirs if k in ours and theirs[k] != ours[k])
    gone = sorted(k for k in ours if k not in theirs and k not in set(dead))
    return new, changed, gone


def counts(src, dst, schema):
    s, d = src.cursor(), dst.cursor()
    s.execute("set statement_timeout = '30min'")
    s.execute("select library_id, count(*) from public.chunks where embedding is not null group by 1")
    theirs = dict(s.fetchall())
    # documents whose passages live on the server only (server_own_index.py) are not Supabase's to compare
    d.execute(f"alter table {schema}.chunk_filters add column if not exists own boolean not null default false")
    d.execute(f"select c.library_id, count(*) from {schema}.chunks c where not exists (select 1 from {schema}.chunk_filters f where f.library_id = c.library_id and f.own) group by 1")
    ours = dict(d.fetchall())
    d.execute(f"select library_id from {schema}.chunk_filters where not live")
    dead = [r[0] for r in d.fetchall()]
    src.rollback(); dst.rollback()
    return theirs, ours, dead


def refresh(src, dst, schema, commit, out=print):
    """The daily refresh. Returns a dict of counts; 'left' is how many documents still differ afterwards."""
    s, d = src.cursor(), dst.cursor()
    theirs, ours, dead = counts(src, dst, schema)
    new, changed, gone = drift(theirs, ours, dead)
    m = dict(new=len(new), changed=len(changed), gone=len(gone), copied=0, skipped=0, left=len(new) + len(changed), switched_off=False)
    out(f">>> documents: {len(theirs):,} in Supabase, {len(ours):,} on the server; new {len(new):,}, changed {len(changed):,}, gone {len(gone):,}", flush=True)
    if not commit:
        return m
    todo = [k for k in new + changed if SAFE_ID.match(k)]
    m["skipped"] = len(new) + len(changed) - len(todo)
    for i in range(0, len(todo), 200):
        batch = todo[i:i + 200]
        d.execute(f"delete from {schema}.chunks where library_id = any(%s)", (batch,))          # the server's copy of these documents, replaced whole
        ids = "{" + ",".join(batch) + "}"
        with s.copy(f"copy (select library_id, page, n, embedding::extensions.halfvec({DIMENSIONS}) from public.chunks where library_id = any('{ids}'::text[]) and embedding is not null) to stdout") as source, \
             d.copy(f"copy {schema}.chunks from stdin") as target:
            for block in source:
                target.write(block)
        dst.commit(); src.rollback()
        m["copied"] += len(batch)
    # every document's company, year and type, and whether Supabase still holds it
    d.execute("create temp table fresh (library_id text primary key, wba_id text, year int, doc_type text) on commit drop")
    with s.copy("copy (select d.library_id, d.wba_id, d.year, d.doc_type from public.documents d where exists (select 1 from public.chunk_docs c where c.library_id = d.library_id)) to stdout") as source, \
         d.copy("copy fresh from stdin") as target:
        for block in source:
            target.write(block)
    d.execute(f"insert into {schema}.chunk_filters (library_id, wba_id, year, doc_type, live) select library_id, wba_id, year, doc_type, true from fresh "
              # a document Supabase holds passages for is Supabase's, whatever its type says today: a report first typed as a
              # policy can be in both (three were, 22 Nov 2026). Left marked own, it was copied every night and never counted,
              # so the copy looked different for ever and the remote search was switched off.
              f"on conflict (library_id) do update set wba_id = excluded.wba_id, year = excluded.year, doc_type = excluded.doc_type, live = true, own = false")
    d.execute(f"update {schema}.chunk_filters f set live = false where f.live and not f.own and not exists (select 1 from fresh x where x.library_id = f.library_id)")
    dst.commit(); src.rollback()
    m["own"] = refresh_own(src, dst, schema)
    theirs, ours, dead = counts(src, dst, schema)
    new, changed, gone = drift(theirs, ours, dead)
    m["left"] = len(new) + len(changed)
    if m["left"]:
        s.execute("update public.search_options set is_on = false, changed = now(), note = note || ' [off by the refresh]' where name = 'meaning_remote' and is_on")
        m["switched_off"] = s.rowcount > 0
        src.commit()
    else:
        # the copy is current again: what a refresh switched off, a refresh switches on. An option the owner switched on
        # ("on since" in its note) and nobody else switched off (a hand writes "off by the owner" in the note to keep it off).
        s.execute("update public.search_options set is_on = true, changed = now(), note = replace(note, ' [off by the refresh]', '') "
                  "where name = 'meaning_remote' and not is_on and note like '%on since%' and note not like '%off by the owner%'")
        m["switched_on"] = s.rowcount > 0
        src.commit()
    return m


def refresh_own(src, dst, schema):
    """The documents kept on the server only: company, year and type from Supabase, and live only while the setting
    own_live is on and Supabase still holds the document. Returns how many there are."""
    s, d = src.cursor(), dst.cursor()
    d.execute(f"select count(*) from {schema}.chunk_filters where own")
    n = d.fetchone()[0]
    if not n:
        dst.rollback()
        return 0
    d.execute(f"create table if not exists {schema}.settings (name text primary key, is_on boolean not null default false, changed timestamptz not null default now())")
    d.execute(f"select coalesce((select is_on from {schema}.settings where name = 'own_live'), false)")
    on = d.fetchone()[0]
    d.execute("create temp table fresh_own (library_id text primary key, wba_id text, year int, doc_type text) on commit drop")
    d.execute(f"select array_agg(distinct doc_type) from {schema}.chunk_filters where own")
    types = [t for t in (d.fetchone()[0] or []) if t and "'" not in t]
    quoted = ",".join("'" + t + "'" for t in types) or "''"
    with s.copy(f"copy (select d.library_id, d.wba_id, d.year, d.doc_type from public.documents d where d.doc_type in ({quoted})) to stdout") as source, \
         d.copy("copy fresh_own from stdin") as target:
        for block in source:
            target.write(block)
    d.execute(f"update {schema}.chunk_filters f set wba_id = x.wba_id, year = x.year, live = %s from fresh_own x where f.own and x.library_id = f.library_id "
              f"and (f.wba_id, f.year, f.live) is distinct from (x.wba_id, x.year, %s)", (on, on))
    d.execute(f"update {schema}.chunk_filters f set live = false where f.own and f.live and not exists (select 1 from fresh_own x where x.library_id = f.library_id)")
    dst.commit(); src.rollback()
    return n


def refresh_report(m, schema, commit):
    lines = [f"### The server's copy of the passages, refreshed (schema {schema})" if commit else f"### The server's copy of the passages (schema {schema}; dry run, nothing copied)", "",
             f"Documents new in Supabase: {m['new']:,}; with a different number of passages: {m['changed']:,}; no longer in Supabase: {m['gone']:,}."]
    if commit:
        lines.append(f"Copied again: {m['copied']:,}; left out for an odd name: {m['skipped']:,}. Documents that still differ: {m['left']:,}.")
        if m["left"]:
            lines.append("The copy is NOT current. The option meaning_remote " + ("was switched off: Supabase's own search carries on." if m["switched_off"] else "was already off."))
        elif m.get("switched_on"):
            lines.append("The copy is current again: the option meaning_remote, switched off by an earlier refresh, is on again.")
    return lines


def switch_compare(src, dst, schema, n, out=print):
    """Supabase's own search with the option off and on, and the exact search on the server with the same filter.
    Returns {variant: {off, on, truth, ms_off, ms_on}}."""
    s, d = src.cursor(), dst.cursor()
    s.execute("select vec from public.query_vectors where array_length(vec, 1) = %s order by at desc limit %s", (DIMENSIONS, n))
    vectors = [r[0] for r in s.fetchall()]
    s.execute("select public.types_matching(%s)", (TYPE,))
    types = s.fetchone()[0]
    src.rollback()
    res = {v: dict(off=[], on=[], truth=[], ms_off=[], ms_on=[]) for v in VARIANTS[:3]}
    exact = (f"with docs as (select f.library_id from {schema}.chunk_filters f where f.live and {conditions()}), "
             f"near as materialized (select c.library_id, c.page, (c.embedding <=> %(q)s::halfvec({DIMENSIONS})) + 0 as dist from {schema}.chunks c "
             f"where %(all)s or c.library_id in (select library_id from docs) order by dist limit {NEAR}), "
             f"pages as (select distinct on (n.library_id, n.page) n.library_id, n.page, n.dist from near n order by n.library_id, n.page, n.dist), "
             f"kept as (select p.*, row_number() over (partition by p.library_id order by p.dist) as in_doc from pages p) "
             f"select x.library_id || '|' || x.page from kept x where x.in_doc <= {PER_DOC} order by x.dist limit 10")
    for vec in vectors:
        qtext = "[" + ",".join(repr(float(x)) for x in vec) + "]"
        for variant in VARIANTS[:3]:
            y0, y1 = YEARS if variant == "two years" else (None, None)
            tf = TYPE if variant == "a type" else None
            try:
                got = {}
                for option in ("off", "on"):
                    s.execute("select set_config('lantern.meaning_remote', %s, false)", (option,))
                    t = time.time()
                    s.execute("select library_id || '|' || page from public.find_meaning('eval', %s::real[], null, %s, %s, %s, 10)", (vec, y0, y1, tf))
                    got[option] = ([r[0] for r in s.fetchall()], (time.time() - t) * 1000)
                    src.rollback()
                d.execute("set max_parallel_workers_per_gather = 6")
                d.execute(exact, dict(q=qtext, y0=y0, y1=y1, types=types if tf else None, all=variant == "no filter"))
                truth = [r[0] for r in d.fetchall()]
                dst.rollback()
                r = res[variant]
                r["off"].append(got["off"][0]); r["on"].append(got["on"][0]); r["truth"].append(truth)
                r["ms_off"].append(got["off"][1]); r["ms_on"].append(got["on"][1])
            except Exception as e:
                src.rollback(); dst.rollback()
                out(f"  one search not compared ({variant}: {type(e).__name__})")
    return res


def switch_report(res):
    md = ["### The switch, on the searches people type: no company, with and without a year or a type", "",
          "| Search | Searches | Of the true first ten: option off | option on | The same ten pages off and on | Pages returned, off / on (ten asked) | ms, off (median / 95th) | ms, on (median / 95th) |", "|---|---|---|---|---|---|---|---|"]
    worse = []
    for v, r in res.items():
        if not r["truth"]:
            md.append(f"| {v} | 0 | | | | | | |"); continue
        off, on = overlap(r["truth"], r["off"]), overlap(r["truth"], r["on"])
        same = sum(1 for a, b in zip(r["off"], r["on"]) if set(a) == set(b))
        full = lambda lists: f"{statistics.mean(len(x) for x in lists):.1f}"
        md.append(f"| {v} | {len(r['truth'])} | {100 * off:.1f}% | {100 * on:.1f}% | {same} | {full(r['off'])} / {full(r['on'])} | "
                  f"{statistics.median(r['ms_off']):.0f} / {percentile(r['ms_off'], 95):.0f} | {statistics.median(r['ms_on']):.0f} / {percentile(r['ms_on'], 95):.0f} |")
        if on < off - 0.03:
            worse.append(v)
    md += ["", "The true first ten are the exact search on the server with the same filter (every passage of the documents the filter allows, no index). "
               + ("No kind of search is lower with the option on by more than 3 points." if not worse else "LOWER with the option on by more than 3 points: " + ", ".join(worse) + ".")]
    return md, worse


def load_report(m, schema, memory, workers):
    gb = lambda b: b / 1024 ** 3
    return [f"### The passages on the server (schema {schema})", "", "| Measured | Value |", "|---|---|",
            f"| Passages copied | {m['rows']:,} of {m['docs']:,} documents in {m['copy_s'] / 60:.1f} min |",
            f"| Documents with their company, year and type | {m['filters']:,}; documents with passages and no such line: {m['orphans']:,} |",
            f"| Documents whose count of passages differs between Supabase and the server | {'not compared (a sample)' if m['differ'] is None else format(m['differ'], ',')} |",
            f"| Table | {gb(m['table_bytes']):.2f} GB |",
            f"| Index | {gb(m['index_bytes']):.2f} GB, built in {m['build_s'] / 60:.1f} min with {memory} and {workers} workers |"]


def compare(src, dst, schema, n, out=print, exact=0):
    """Stage A. Returns {variant: {theirs, ours, ms_server, ms_supabase, short, filled}} over n kept question vectors."""
    s, d = src.cursor(), dst.cursor()
    s.execute("select vec from public.query_vectors where array_length(vec, 1) = %s order by at desc limit %s", (DIMENSIONS, n))
    vectors = [r[0] for r in s.fetchall()]
    s.execute("select public.types_matching(%s)", (TYPE,))
    types = s.fetchone()[0]
    d.execute(f"set hnsw.ef_search = {EF_SEARCH}")
    res = {v: dict(theirs=[], ours=[], ms_server=[], ms_supabase=[], short=0, filled=0) for v in VARIANTS}
    res["no filter"]["exact"] = []
    for number, vec in enumerate(vectors):
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
                if variant == "no filter" and number < exact:          # the truth both indexes are approximations of
                    d.execute(exact_sql(schema), dict(q=qtext))
                    r["exact"].append(([g[0] for g in got], ours, [x[0] for x in d.fetchall()]))
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
    truth = res["no filter"].get("exact") or []
    if truth:
        of = lambda i: 100 * overlap([t[2] for t in truth], [t[i] for t in truth])
        md += ["", f"Against an exact search (every passage read, no index), over {len(truth)} searches with no filter: Supabase's index returns {of(0):.1f}% of the true first ten, "
                   f"the server's index {of(1):.1f}%. Two indexes built apart are two approximations of the same truth: they need not agree with each other, and each is judged against the truth."]
    return md, ov, med, p95


def main(argv=None):
    ap = argparse.ArgumentParser(description="The passages on the server's database and the comparison with Supabase.")
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="bring the server's copy up to date (needs --commit)")
    ap.add_argument("--switch", type=int, default=0, help="kept question vectors to ask of Supabase's search with the option off and on")
    ap.add_argument("--compare", type=int, default=0, help="kept question vectors to ask both ways")
    ap.add_argument("--exact", type=int, default=0, help="with --compare: for this many of them, also the exact search on the server, to judge both indexes against the truth")
    ap.add_argument("--sample", type=float, default=0, help="with --load: a block sample of this percent, into the schema meaning_trial")
    ap.add_argument("--trial", action="store_true", help="with --compare: read the schema meaning_trial")
    ap.add_argument("--reload", action="store_true", help="with --load: empty the server's copy first")
    ap.add_argument("--memory", default="8GB")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"\d{1,3}(MB|GB)", a.memory) or not 1 <= a.workers <= 12 or not 0 <= a.sample <= 100 or not 0 <= a.compare <= 2000 or not 0 <= a.switch <= 500:
        sys.exit("--memory like 8GB; --workers 1 to 12; --sample 0 to 100; --compare 0 to 2,000.")
    if not os.environ.get("SUPABASE_DB_URL") or not os.environ.get("LOCAL_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL and LOCAL_DB_URL.")
    schema = "meaning_trial" if (a.sample or a.trial) else "meaning"
    import psycopg
    run = qa.Run("server_meaning.py")
    md, counters, stale = [], {}, False
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
            if a.refresh:
                m = refresh(src, dst, schema, a.commit)
                md += ([""] if md else []) + refresh_report(m, schema, a.commit)
                counters.update(new=m["new"], changed=m["changed"], gone=m["gone"], copied=m["copied"], left=m["left"])
                if a.commit and m["left"]:
                    run.failure("RUN_FAILED", f"server_meaning.py: the server's copy of the passages is not current ({m['left']} documents differ)", key="server_meaning_refresh")
                    stale = True
            if a.switch:
                res = switch_compare(src, dst, schema, a.switch)
                lines, worse = switch_report(res)
                md += ([""] if md else []) + lines
                counters.update(switch_worse=len(worse))
            if a.compare:
                res = compare(src, dst, schema, a.compare, exact=a.exact)
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
    run.finish(status="failed" if stale else "ok", rows_in=counters.get("rows", 0), rows_out=counters.get("rows", 0), counters=counters)
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
