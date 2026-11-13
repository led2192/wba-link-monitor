#!/usr/bin/env python3
"""
fiscal_months.py: the month each company's fiscal year ends, kept in the search database.

Why (13 Nov 2026): WBA counts a fiscal year in the calendar year where at least six months of it fall
(reporting_year.py), so a company whose year ends in May has its "fiscal 2024" as reporting year 2023. The reader
worked the month out at every run (extract_eval.fy_end_month) and did not keep it. lanterN then showed "2019" for
a figure the company's table prints under FY20, and an answer said "fiscal 2023" without saying which reporting
year that is. This script writes the month to public.company_fiscal, where find_values, Ask and the facts of
answers read it.

The month: the commonest month among the period ends of the company's annual-family documents
(documents.period_end); when none carries one, read from the first twelve pages of up to three of its latest such
documents (fiscal_year.decide), as the reader does. A company whose month cannot be found gets a row with no
month, so it is not looked for again at every run; such rows are tried again after 30 days.

Order: companies that have values or facts of answers first (their cards are the ones people see), then the rest.

    python fiscal_months.py [--limit 400] [--minutes 20] [--refresh] [--commit]

Dry run prints what would be written. Env: SUPABASE_DB_URL; AIRTABLE_QA_TOKEN for the run record (QA_COMMIT is
set by --commit).
"""
import argparse, os, sys, time

import qa

TODO_SQL = """
with seen as (
  select wba_id, 0 as rank from public.value_versions where wba_id is not null
  union all select wba_id, 0 from public.ask_facts where wba_id is not null
  union all select wba_id, 1 from public.documents where wba_id is not null)
select s.wba_id from seen s left join public.company_fiscal f on f.wba_id = s.wba_id
where %(refresh)s or f.wba_id is null or (f.fy_end_month is null and f.at < now() - interval '30 days')
group by s.wba_id order by min(s.rank), s.wba_id limit %(limit)s"""


def month_of(cur, wba, read_pages):
    """(month or None, source). From the documents' period ends first; else from the pages (read_pages(cur, wba))."""
    cur.execute("select substr(period_end, 6, 2), count(*) from public.documents where wba_id = %s "
                "and period_end ~ '^[0-9]{4}-[0-9]{2}$' group by 1 order by 2 desc, 1 limit 1", (wba,))
    row = cur.fetchone()
    if row and row[0] and 1 <= int(row[0]) <= 12:
        return int(row[0]), "period_end"
    m = read_pages(cur, wba)
    return (int(m), "pages") if m and 1 <= int(m) <= 12 else (None, None)


def run(conn, read_pages, limit=400, minutes=20, refresh=False, commit=False, out=print, clock=time.time):
    """Counters of one pass: looked, found, period_end, pages, none, written."""
    cur = conn.cursor()
    cur.execute(TODO_SQL, {"refresh": bool(refresh), "limit": int(limit)})
    todo = [r[0] for r in cur.fetchall()]
    c = dict(looked=0, found=0, period_end=0, pages=0, none=0, written=0)
    t0 = clock()
    for wba in todo:
        if clock() - t0 > minutes * 60:
            out(f"Stopped at the time budget of {minutes} minutes; the rest waits for the next run.")
            break
        try:
            month, source = month_of(cur, wba, read_pages)
        except Exception as e:                                # one company's failure is not the run's
            conn.rollback()
            out(f"  {wba}: not read ({type(e).__name__})")
            continue
        c["looked"] += 1
        if month:
            c["found"] += 1; c[source] += 1
        else:
            c["none"] += 1
        if commit:
            cur.execute("insert into public.company_fiscal (wba_id, fy_end_month, source, at) values (%s, %s, %s, now()) "
                        "on conflict (wba_id) do update set fy_end_month = excluded.fy_end_month, source = excluded.source, at = now()",
                        (wba, month, source))
            conn.commit(); c["written"] += 1
    out(f"{len(todo)} companies to look at; {c['looked']} looked at: {c['found']} with a month ({c['period_end']} from period ends, "
        f"{c['pages']} from pages), {c['none']} with none. " + (f"{c['written']} rows written." if commit else "Dry run: nothing written."))
    return c


def main(argv=None):
    ap = argparse.ArgumentParser(description="The month each company's fiscal year ends, kept in the search database.")
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--minutes", type=int, default=20)
    ap.add_argument("--refresh", action="store_true", help="look again at companies that already have a row")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args(argv)
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    import psycopg
    import extract_eval
    log = qa.Run("fiscal_months.py")
    try:
        with psycopg.connect(os.environ["SUPABASE_DB_URL"]) as conn:
            c = run(conn, extract_eval.fy_end_month, a.limit, a.minutes, a.refresh, a.commit)
    except Exception as e:
        log.failure("RUN_FAILED", f"fiscal_months.py stopped: {type(e).__name__}", key="fiscal_months", message=str(e)[:300])
        log.finish(status="failed")
        raise
    log.finish(status="ok", rows_in=c["looked"], rows_out=c["written"], counters=c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
