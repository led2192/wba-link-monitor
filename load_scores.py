#!/usr/bin/env python3
"""
load_scores.py: the companies' benchmark results from the WBA Data API into the evidence database, for
lanterN's company card.

The owner wants, beside a search for a company, its results of the benchmarks as the Data Explorer shows them
(data.worldbenchmarkingalliance.org): the score, the place within each benchmark and within its industry.
The Data API (https://data.worldbenchmarkingalliance.org/api/data/v1, "Authorization: Bearer <key>", one
request a second, 1,000 rows a page) serves the same datasets; its company ids are our wba_id (PT_...).

What it reads: the benchmark scores (dataset benchmarks, about 10,000 rows) and the measurement area scores
(measurementareas, about 27,000 rows), ONLY the columns named in BENCH_COLS and AREA_COLS (the API's `include`
parameter). The datasets also carry a contact person per company: that column is never asked for, and nothing
this script prints is a row's content (the repository's logs are public; the first version printed a sample
row with those addresses, 10 Nov 2026). What it writes: public.company_scores and public.company_area_scores,
upsert by the API's own score id; never deletes. Ranks are not stored: the search function counts them when
asked, within the benchmark and year, and within the company's industry.

    python load_scores.py [--commit]

Dry run reads both datasets and prints counts by benchmark and year. Env: WBA_DATA_API_KEY (repository
secret), SUPABASE_DB_URL; AIRTABLE_QA_TOKEN for the run record (QA_COMMIT=true writes it).
"""
import argparse, collections, os, sys, time

BASE = "https://data.worldbenchmarkingalliance.org/api/data/v1"
PAUSE = 1.1                                  # the API allows one request a second per key
PER_PAGE = 1000
BENCH_COLS = ["benchmark_score_id", "company_id", "benchmark_id", "benchmark_name", "methodology_id", "methodology_year",
              "transformation_name", "benchmark_score_numerical", "benchmark_score_categorical", "updated"]
AREA_COLS = ["measurement_area_score_id", "company_id", "benchmark_id", "methodology_id", "methodology_year",
             "measurement_area_id", "measurement_area_name", "measurement_area_score_numerical", "updated"]


def get(path, key, params=None, session=None, sleep=time.sleep):
    """The JSON of GET BASE+path; waits PAUSE first (the rate limit); raises with the status and the API's detail."""
    import requests
    sleep(PAUSE)
    r = (session or requests).get(BASE + path, headers={"Authorization": f"Bearer {key}"}, params=params or {}, timeout=90)
    if r.status_code != 200:
        detail = ""
        try:
            detail = str(r.json().get("detail", ""))[:200]
        except Exception:
            pass
        raise RuntimeError(f"GET {path}: HTTP {r.status_code} {detail}")
    return r.json()


def rows_of(payload):
    """The row objects of a dataset response ({'data': [...], 'meta': {...}}), or of a bare list."""
    if isinstance(payload, dict):
        return payload.get("data") or [], payload.get("meta") or {}
    return payload or [], {}


def fetch_all(slug, cols, key, order_by, session=None, sleep=time.sleep, max_pages=200):
    """Every row of a dataset, the named columns only, in pages of PER_PAGE ordered by its id (a stable order)."""
    out = []
    for page in range(1, max_pages + 1):
        rows, _ = rows_of(get(f"/datasets/{slug}", key, {"include": ",".join(cols), "per_page": PER_PAGE, "page": page,
                                                         "order_by": order_by, "ascending": "true"}, session, sleep))
        out += rows
        if len(rows) < PER_PAGE:
            break
    return out


def year_of(v):
    try:
        return int(str(v)[:4])
    except (TypeError, ValueError):
        return None


def num(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def bench_rows(rows):
    """[(score_id, wba_id, benchmark_id, benchmark_name, methodology_id, year, transformation, score, category, updated)]; rows without an id or a company are skipped."""
    out = {}
    for r in rows:
        sid, co = (r.get("benchmark_score_id") or "").strip(), (r.get("company_id") or "").strip()
        if sid and co:
            out[sid] = (sid, co, r.get("benchmark_id"), r.get("benchmark_name"), r.get("methodology_id"), year_of(r.get("methodology_year")),
                        r.get("transformation_name"), num(r.get("benchmark_score_numerical")), r.get("benchmark_score_categorical"), r.get("updated"))
    return list(out.values())


def area_rows(rows):
    """[(score_id, wba_id, benchmark_id, methodology_id, year, area_id, area_name, score, updated)]."""
    out = {}
    for r in rows:
        sid, co = (r.get("measurement_area_score_id") or "").strip(), (r.get("company_id") or "").strip()
        if sid and co:
            out[sid] = (sid, co, r.get("benchmark_id"), r.get("methodology_id"), year_of(r.get("methodology_year")), r.get("measurement_area_id"),
                        r.get("measurement_area_name"), num(r.get("measurement_area_score_numerical")), r.get("updated"))
    return list(out.values())


DDL = """
create table if not exists public.company_scores (
  score_id text primary key, wba_id text not null, benchmark_id text, benchmark_name text, methodology_id text, year int,
  transformation text, score numeric, category text, source_updated timestamptz, loaded timestamptz not null default now());
create index if not exists company_scores_company on public.company_scores (wba_id);
create index if not exists company_scores_method on public.company_scores (methodology_id);
alter table public.company_scores enable row level security;
create table if not exists public.company_area_scores (
  score_id text primary key, wba_id text not null, benchmark_id text, methodology_id text, year int, area_id text, area_name text,
  score numeric, source_updated timestamptz, loaded timestamptz not null default now());
create index if not exists company_area_scores_company on public.company_area_scores (wba_id, methodology_id);
alter table public.company_area_scores enable row level security;
"""


def upsert(conn, bench, areas):
    with conn.cursor() as c:
        c.execute("set lock_timeout = '30s'")
        c.execute(DDL)
        for i in range(0, len(bench), 1000):
            c.executemany("""insert into public.company_scores (score_id, wba_id, benchmark_id, benchmark_name, methodology_id, year, transformation, score, category, source_updated)
                             values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                             on conflict (score_id) do update set wba_id = excluded.wba_id, benchmark_id = excluded.benchmark_id, benchmark_name = excluded.benchmark_name,
                               methodology_id = excluded.methodology_id, year = excluded.year, transformation = excluded.transformation, score = excluded.score,
                               category = excluded.category, source_updated = excluded.source_updated, loaded = now()""", bench[i:i + 1000])
        for i in range(0, len(areas), 1000):
            c.executemany("""insert into public.company_area_scores (score_id, wba_id, benchmark_id, methodology_id, year, area_id, area_name, score, source_updated)
                             values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                             on conflict (score_id) do update set wba_id = excluded.wba_id, benchmark_id = excluded.benchmark_id, methodology_id = excluded.methodology_id,
                               year = excluded.year, area_id = excluded.area_id, area_name = excluded.area_name, score = excluded.score,
                               source_updated = excluded.source_updated, loaded = now()""", areas[i:i + 1000])
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    key = (os.environ.get("WBA_DATA_API_KEY") or "").strip()
    if not key:
        print("WBA_DATA_API_KEY is not set: nothing read. Add the Data API key as a repository secret with that name.")
        return 0
    import qa
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("load_scores.py")
    t0 = time.time()
    try:
        bench = bench_rows(fetch_all("benchmarks", BENCH_COLS, key, "benchmark_score_id"))
        areas = area_rows(fetch_all("measurementareas", AREA_COLS, key, "measurement_area_score_id"))
    except Exception as e:
        run.failure("RUN_FAILED", f"the Data API refused or failed: {e}", key="load_scores")
        run.finish("failed", notes=str(e)[:200])
        print(f"Stopped: {e}")
        return 1
    by = collections.Counter((r[4] or "(none)") for r in bench)
    scored = sum(1 for r in bench if r[7] is not None)
    md = ["### Benchmark scores (" + ("written" if a.commit else "dry run") + ")", "",
          f"{len(bench):,} benchmark scores ({scored:,} with a number) for {len({r[1] for r in bench}):,} companies and {len(areas):,} measurement area scores, "
          f"read in {time.time() - t0:.0f} s.", "", "Scores by methodology: " + ", ".join(f"{k} {n}" for k, n in sorted(by.items())), ""]
    if a.commit:
        if not os.environ.get("SUPABASE_DB_URL"):
            sys.exit("Set SUPABASE_DB_URL.")
        import evidence_db
        with evidence_db.connect() as conn:
            upsert(conn, bench, areas)
        md.append(f"{len(bench):,} rows upserted into public.company_scores and {len(areas):,} into public.company_area_scores.")
    out = "\n".join(md) + "\n"
    print(out, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(out)
    run.finish("ok", rows_in=len(bench) + len(areas), rows_out=(len(bench) + len(areas)) if a.commit else 0,
               counters={"benchmark_scores": len(bench), "with_number": scored, "area_scores": len(areas), "methodologies": len(by)},
               notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
