#!/usr/bin/env python3
"""
server_rehearsal.py: a small rehearsal of the search by meaning on the server's own database.

Step 3 of the move (docs/server.md, "The database"), the part that needs no public name. It answers, with numbers
measured on the machine and not assumed:

  - how fast passages travel from Supabase to the server (rows a second, GB a million)
  - how long the index of meanings takes to build for a million passages, and how large it is
  - how good and how fast that index is: of the true ten nearest passages (an exact search), how many the index
    returns, and the time of a search (median and 95th percentile)

from which the full move is extrapolated: 4.1 million passages (like for like with Supabase today) and about 21
million (every year). The extrapolation of the build time is a floor, not a forecast: an index build slows once its
graph no longer fits the memory it is given.

What it touches:
  Supabase: reads public.chunks (a block sample), nothing else, writes nothing.
  The server's database: its own schema "rehearsal", created and, at the end, dropped (unless --keep). Nothing else.

It prints counts, sizes and times only: no document, page or text.

    python server_rehearsal.py                 what would be done, and the sizes on both sides (reads only)
    python server_rehearsal.py --commit        the rehearsal
      [--rows 1000000] [--queries 100] [--memory 8GB] [--workers 6] [--keep]

Env: SUPABASE_DB_URL (read), LOCAL_DB_URL (the server's database, a role that may create the schema "rehearsal").
"""
import argparse, math, os, statistics, sys, time

import qa

SCHEMA = "rehearsal"
DIMENSIONS = 384
FULL_TODAY = 4_100_000          # passages Supabase searches today
FULL_ALL_YEARS = 21_000_000     # passages once every year is in
EF_SEARCH = 200                 # as find_meaning sets it in Supabase (search.sql)


def percentile(values, p):
    """The p-th percentile (nearest rank) of a list; 0 when empty."""
    v = sorted(values)
    return v[min(len(v) - 1, max(0, math.ceil(p / 100 * len(v)) - 1))] if v else 0


def sample_percent(rows, total):
    """The share of the table's blocks to read for about `rows` rows, with a tenth to spare; 100 at most."""
    return 100.0 if total <= 0 else round(min(100.0, 110.0 * rows / total), 3)


def recall(exact, found):
    """Of the keys in each exact list, the share present in the list found for the same query, over all queries."""
    want = sum(len(e) for e in exact)
    return sum(len(set(e) & set(f)) for e, f in zip(exact, found)) / want if want else 0.0


def extrapolate(minutes, rows, target):
    """Build minutes for `target` rows at the measured rate: a floor (a build is slower than linear)."""
    return minutes * target / rows if rows else 0.0


def copy_sample(src, dst, rows, out=print):
    """About `rows` passages (keys and half-precision embeddings) from Supabase into rehearsal.chunks. Returns (rows, seconds)."""
    s, d = src.cursor(), dst.cursor()
    s.execute("select reltuples::bigint from pg_class where oid = 'public.chunks'::regclass")
    total = s.fetchone()[0]
    pct = sample_percent(rows, total)
    out(f">>> Supabase holds about {total:,} passages; reading a block sample of {pct}% for {rows:,}")
    s.execute("set statement_timeout = '45min'")
    d.execute(f"create schema if not exists {SCHEMA}")
    d.execute(f"drop table if exists {SCHEMA}.chunks")
    d.execute(f"create table {SCHEMA}.chunks (library_id text not null, page int not null, n int not null, embedding halfvec({DIMENSIONS}) not null)")
    t = time.time()
    take = (f"copy (select library_id, page, n, embedding::extensions.halfvec({DIMENSIONS}) from public.chunks tablesample system ({pct}) "
            f"where embedding is not null limit {int(rows)}) to stdout")
    with s.copy(take) as source, d.copy(f"copy {SCHEMA}.chunks from stdin") as target:
        for block in source:
            target.write(block)
    dst.commit()
    src.rollback()
    d.execute(f"select count(*) from {SCHEMA}.chunks")
    return d.fetchone()[0], time.time() - t


def build_index(dst, memory, workers):
    """The index of meanings on the rehearsal table, as Supabase defines its own. Returns seconds."""
    d = dst.cursor()
    d.execute(f"set maintenance_work_mem = '{memory}'")
    d.execute(f"set max_parallel_maintenance_workers = {int(workers)}")
    t = time.time()
    d.execute(f"create index rehearsal_embedding on {SCHEMA}.chunks using hnsw (embedding halfvec_cosine_ops) with (m = 16, ef_construction = 64)")
    dst.commit()
    return time.time() - t


def measure(dst, queries):
    """`queries` passages of the table asked as questions, each by the index and exactly. Returns (recall of the
    first ten, [milliseconds a search by the index])."""
    d = dst.cursor()
    d.execute(f"select embedding::text from {SCHEMA}.chunks tablesample system (1) limit {int(queries)}")
    asked = [r[0] for r in d.fetchall()]
    q = f"select library_id || '|' || page || '|' || n from {SCHEMA}.chunks order by embedding <=> %s::halfvec({DIMENSIONS}) limit 10"
    d.execute(f"set hnsw.ef_search = {EF_SEARCH}")
    found, ms = [], []
    for v in asked:
        t = time.time()
        d.execute(q, (v,))
        found.append([r[0] for r in d.fetchall()])
        ms.append((time.time() - t) * 1000)
    d.execute("set enable_indexscan = off")
    d.execute("set enable_bitmapscan = off")
    exact = []
    for v in asked:
        d.execute(q, (v,))
        exact.append([r[0] for r in d.fetchall()])
    d.execute("reset enable_indexscan")
    d.execute("reset enable_bitmapscan")
    dst.commit()
    return recall(exact, found), ms


def report(rows, copy_s, table_bytes, build_s, index_bytes, rec, ms, memory, workers):
    gb = lambda b: b / 1024 ** 3
    build_min = build_s / 60
    scale = lambda target: target / rows if rows else 0
    return [
        "### Rehearsal of the search by meaning on the server", "",
        "| Measured | Value |", "|---|---|",
        f"| Passages copied from Supabase | {rows:,} in {copy_s / 60:.1f} min ({rows / copy_s:,.0f} a second) |" if copy_s else f"| Passages copied | {rows:,} |",
        f"| Table on the server | {gb(table_bytes):.2f} GB ({gb(table_bytes) * scale(1_000_000):.2f} GB a million) |",
        f"| Index build | {build_min:.1f} min with {memory} and {workers} workers |",
        f"| Index size | {gb(index_bytes):.2f} GB |",
        f"| Of the true ten nearest, found by the index | {100 * rec:.1f}% over {len(ms)} searches |",
        f"| A search by the index | median {statistics.median(ms):.1f} ms, 95th percentile {percentile(ms, 95):.1f} ms, slowest {max(ms):.1f} ms |" if ms else "| A search by the index | not measured |",
        "",
        "| Extrapolated (a floor for the build: it is slower than linear once the graph outgrows its memory) | 4.1 million (today, like for like) | 21 million (every year) |", "|---|---|---|",
        f"| Copy | {copy_s / 60 * scale(FULL_TODAY):.0f} min | {copy_s / 3600 * scale(FULL_ALL_YEARS):.1f} h |",
        f"| Table | {gb(table_bytes) * scale(FULL_TODAY):.1f} GB | {gb(table_bytes) * scale(FULL_ALL_YEARS):.1f} GB |",
        f"| Index | {gb(index_bytes) * scale(FULL_TODAY):.1f} GB | {gb(index_bytes) * scale(FULL_ALL_YEARS):.1f} GB |",
        f"| Index build, at least | {extrapolate(build_min, rows, FULL_TODAY):.0f} min | {extrapolate(build_min, rows, FULL_ALL_YEARS) / 60:.1f} h |",
    ]


def main(argv=None):
    ap = argparse.ArgumentParser(description="A small rehearsal of the search by meaning on the server's own database.")
    ap.add_argument("--rows", type=int, default=1_000_000)
    ap.add_argument("--queries", type=int, default=100)
    ap.add_argument("--memory", default="8GB", help="maintenance_work_mem for the index build")
    ap.add_argument("--workers", type=int, default=6, help="parallel workers for the index build")
    ap.add_argument("--keep", action="store_true", help="leave the rehearsal schema in place at the end")
    ap.add_argument("--commit", action="store_true", help="run the rehearsal (default: say what would be done)")
    a = ap.parse_args(argv)
    import re
    if not re.fullmatch(r"\d{1,3}(MB|GB)", a.memory) or not 1 <= a.workers <= 12 or not 1000 <= a.rows <= 5_000_000 or not 1 <= a.queries <= 1000:
        sys.exit("--memory like 8GB; --workers 1 to 12; --rows 1,000 to 5,000,000; --queries 1 to 1,000.")
    if not os.environ.get("SUPABASE_DB_URL") or not os.environ.get("LOCAL_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL and LOCAL_DB_URL.")
    import psycopg
    run = qa.Run("server_rehearsal.py")
    try:
        with psycopg.connect(os.environ["SUPABASE_DB_URL"]) as src, psycopg.connect(os.environ["LOCAL_DB_URL"]) as dst:
            d = dst.cursor()
            d.execute("select current_setting('server_version'), (select extversion from pg_extension where extname = 'vector'), pg_database_size(current_database())")
            version, vector, size = d.fetchone()
            print(f">>> the server's database: PostgreSQL {version}, pgvector {vector}, {size / 1024 ** 2:.0f} MB")
            if not a.commit:
                s = src.cursor()
                s.execute("select reltuples::bigint, pg_total_relation_size('public.chunks') from pg_class where oid = 'public.chunks'::regclass")
                total, bytes_ = s.fetchone()
                print(f">>> dry run: would copy about {a.rows:,} of {total:,} passages ({bytes_ / 1024 ** 3:.1f} GB with its indexes in Supabase), "
                      f"build their index with {a.memory} and {a.workers} workers, ask {a.queries} searches, and drop the schema {SCHEMA}"
                      f"{' (kept: --keep)' if a.keep else ''}. Nothing done. --commit runs it.")
                run.finish(status="ok", rows_in=0, rows_out=0)
                return 0
            rows, copy_s = copy_sample(src, dst, a.rows)
            d.execute(f"select pg_table_size('{SCHEMA}.chunks')")
            table_bytes = d.fetchone()[0]
            print(f">>> copied {rows:,} passages in {copy_s / 60:.1f} min; building the index", flush=True)
            build_s = build_index(dst, a.memory, a.workers)
            d.execute("select pg_relation_size('rehearsal.rehearsal_embedding')")
            index_bytes = d.fetchone()[0]
            print(f">>> index built in {build_s / 60:.1f} min; asking {a.queries} searches", flush=True)
            rec, ms = measure(dst, a.queries)
            md = report(rows, copy_s, table_bytes, build_s, index_bytes, rec, ms, a.memory, a.workers)
            if not a.keep:
                d.execute(f"drop schema {SCHEMA} cascade")          # the rehearsal's own tables, and only those
                dst.commit()
                md += ["", f"The schema {SCHEMA} was dropped at the end; the server's database is empty again."]
            text = "\n".join(md)
            print(text)
            if os.environ.get("GITHUB_STEP_SUMMARY"):
                open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8").write(text + "\n")
    except Exception as e:
        run.failure("RUN_FAILED", f"server_rehearsal.py stopped: {type(e).__name__}", key="server_rehearsal", message=str(e)[:300])
        run.finish(status="failed")
        raise
    run.finish(status="ok", rows_in=rows, rows_out=rows,
               counters={"rows": rows, "copy_s": round(copy_s), "build_s": round(build_s), "index_mb": round(index_bytes / 1024 ** 2),
                         "recall_pct": round(100 * rec, 1), "median_ms": round(statistics.median(ms), 1) if ms else 0})
    return 0


if __name__ == "__main__":
    sys.exit(main())
