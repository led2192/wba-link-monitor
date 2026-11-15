#!/usr/bin/env python3
"""
meaning_bits_compare.py: does a search by meaning through the small index on the bits find the pages the full index finds?

The index of meanings (chunks_embedding, half-precision vectors) is 4 GB for 4 million passages and would be about
17 GB for every year: more than the memory of the everyday database machine. chunks_bits holds one bit a number and
is about a sixteenth of that. This script takes the query vectors people's searches left (public.query_vectors), and
for each asks both ways for the nearest pages with no company named:

  full   as find_meaning does today: the 400 nearest passages through chunks_embedding, their pages in order
  bits   the 800 passages nearest by bits through chunks_bits, ordered again by the whole embeddings, the best 400

and reports how many of the full index's first 10 and first 50 pages the bits find, how often the first page is the
same, and the time of each. Calls no model. Reads only. The log is public: counts, shares and milliseconds only.

    python meaning_bits_compare.py [--limit 200] [--candidates 800]

Env: SUPABASE_DB_URL; AIRTABLE_QA_TOKEN for the run record (QA_COMMIT).
"""
import argparse, os, statistics, sys, time

import qa

FULL_SQL = """
with near as materialized (
  select c.library_id, c.page, (c.embedding::extensions.halfvec(384)) operator(extensions.<=>) %s::real[]::extensions.vector(384)::extensions.halfvec(384) as dist
  from public.chunks c
  order by (c.embedding::extensions.halfvec(384)) operator(extensions.<=>) %s::real[]::extensions.vector(384)::extensions.halfvec(384) limit 400)
select library_id, page from (select distinct on (library_id, page) library_id, page, dist from near order by library_id, page, dist) p
order by dist limit 50"""

BITS_SQL = """
with cand as materialized (
  select c.library_id, c.page, c.embedding
  from public.chunks c
  order by (extensions.binary_quantize(c.embedding)::bit(384)) operator(extensions.<~>) extensions.binary_quantize(%s::real[]::extensions.vector(384))::bit(384) limit %s),
near as (select library_id, page, embedding operator(extensions.<=>) %s::real[]::extensions.vector(384) as dist from cand order by dist limit 400)
select library_id, page from (select distinct on (library_id, page) library_id, page, dist from near order by library_id, page, dist) p
order by dist limit 50"""


def overlap(full, bits, k):
    """Of the full index's first k pages, the share the bits' first k hold; None when the full index found nothing."""
    a = full[:k]
    if not a:
        return None
    b = set(bits[:k])
    return sum(1 for x in a if x in b) / len(a)


def summarise(rows):
    """rows: [(full pages, bits pages, ms full, ms bits)] -> the report's numbers."""
    ok = [r for r in rows if r[0]]
    mean = lambda xs: sum(xs) / len(xs) if xs else 0.0
    return {"queries": len(rows), "with_pages": len(ok),
            "at10": mean([overlap(f, b, 10) for f, b, _, _ in ok]), "at50": mean([overlap(f, b, 50) for f, b, _, _ in ok]),
            "first_same": mean([1.0 if b and f[0] == b[0] else 0.0 for f, b, _, _ in ok]),
            "ms_full": statistics.median([r[2] for r in rows]) if rows else 0, "ms_bits": statistics.median([r[3] for r in rows]) if rows else 0,
            "ms_full_max": max([r[2] for r in rows], default=0), "ms_bits_max": max([r[3] for r in rows], default=0)}


def report(s, candidates):
    return "\n".join([
        "### Search by meaning: the small index on the bits beside the full index", "",
        f"{s['queries']} query vectors of past searches, no company named; {s['with_pages']} found pages. Bits: {candidates} candidates, ordered again by the whole embeddings.", "",
        "| | value |", "|---|---|",
        f"| of the full index's first 10 pages, found by the bits | {100 * s['at10']:.1f}% |",
        f"| of its first 50 pages | {100 * s['at50']:.1f}% |",
        f"| first page the same | {100 * s['first_same']:.1f}% |",
        f"| time, full index (median, slowest) | {s['ms_full']:.0f} ms, {s['ms_full_max']:.0f} ms |",
        f"| time, bits (median, slowest) | {s['ms_bits']:.0f} ms, {s['ms_bits_max']:.0f} ms |", ""])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--candidates", type=int, default=800)
    a = ap.parse_args()
    if not 100 <= a.candidates <= 1000:
        sys.exit("candidates: 100 to 1000 (the index returns at most hnsw.ef_search rows, 1000 at most)")
    import psycopg
    run = qa.Run("meaning_bits_compare.py")
    rows = []
    with psycopg.connect(os.environ["SUPABASE_DB_URL"]) as conn:
        cur = conn.cursor()
        cur.execute("select to_regclass('public.chunks_embedding') is not null, to_regclass('public.chunks_bits') is not null")
        full_there, bits_there = cur.fetchone()
        if not (full_there and bits_there):
            print(f"Not compared: chunks_embedding {'is' if full_there else 'is NOT'} there, chunks_bits {'is' if bits_there else 'is NOT'} there.", flush=True)
            run.finish("ok", rows_in=0, rows_out=0, notes="an index is missing")
            return 0
        cur.execute("select vec from public.query_vectors where array_length(vec, 1) = 384 order by at desc limit %s", (a.limit,))
        vecs = [r[0] for r in cur.fetchall()]
        cur.execute("set statement_timeout = '60s'")
        for v in vecs:
            cur.execute("set hnsw.ef_search = 200")
            t = time.time(); cur.execute(FULL_SQL, (v, v)); full = [tuple(r) for r in cur.fetchall()]; ms_full = (time.time() - t) * 1000
            cur.execute(f"set hnsw.ef_search = {a.candidates}")
            t = time.time(); cur.execute(BITS_SQL, (v, a.candidates, v)); bits = [tuple(r) for r in cur.fetchall()]; ms_bits = (time.time() - t) * 1000
            rows.append((full, bits, ms_full, ms_bits))
    s = summarise(rows)
    text = report(s, a.candidates)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    run.finish("ok", rows_in=len(vecs), rows_out=s["with_pages"], notes=f"bits beside full: {100 * s['at10']:.0f}% of the first 10",
               counters={k: round(v, 3) if isinstance(v, float) else v for k, v in s.items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
