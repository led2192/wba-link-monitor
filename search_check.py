#!/usr/bin/env python3
"""
search_check.py: read-only check of the evidence database's search functions (supabase/search.sql):
does each sample search answer, how many companies, documents and pages it finds, and how long it takes.
Prints counts, timings and the first result's company and page, never page text (the logs are public).

Run by the Search check workflow on pull requests that touch the search functions, and by hand.
Env: SUPABASE_DB_URL.
"""
import os, sys, time, traceback

SEARCHES = [
    # (label, function, arguments)
    ("phrase: \"living wage\"", "pages", ('"living wage"',)),
    ("acronym: TNFD", "pages", ("TNFD",)),
    ("phrase: \"scope 3\"", "pages", ('"scope 3"',)),
    ("common word: climate", "pages", ("climate",)),
    ("two words, 2025 reports only: grievance mechanism", "pages", ("grievance mechanism", None, 2025, 2025)),
    ("company filter: \"net zero\" at Unilever", "pages", ('"net zero"', "Unilever")),
    ("other language: \"développement durable\"", "pages", ('"développement durable"',)),
    ("WBA: \"World Benchmarking Alliance\"", "pages", ('"World Benchmarking Alliance"',)),
    ("evidence passages, nature: water withdrawal", "passages", ("water withdrawal", "nature")),
    ("evidence passages, social, no words", "passages", ("", "social")),
]


def run(cur, fn, args):
    t0 = time.time()
    if fn == "pages":
        a = list(args) + [None] * (5 - len(args))
        cur.execute("select * from public.search_summary(%s, %s, %s, %s, %s)", a)
        summary = cur.fetchone()
        t1 = time.time()
        cur.execute("select company, year, page from public.search_pages(%s, %s, %s, %s, %s, 50)", a)
    else:
        summary = None
        t1 = t0
        cur.execute("select company, year, page from public.search_passages(%s, %s, null, null, null, 50)", list(args))
    rows = cur.fetchall()
    return summary, rows, t1 - t0, time.time() - t1


def main():
    import psycopg
    md = ["### Search check", "", "Read-only. Times are seconds from GitHub's servers to Frankfurt, network included.", "",
          "| Search | Companies | Documents | Pages | Count time | Results shown | Results time | First result |",
          "| --- | --: | --: | --: | --: | --: | --: | --- |"]
    bad = 0
    with psycopg.connect(os.environ["SUPABASE_DB_URL"].strip(), connect_timeout=30) as conn:
        cur = conn.cursor()
        cur.execute("set statement_timeout = '120s'")
        for label, fn, args in SEARCHES:
            try:
                s, rows, tc, tr = run(cur, fn, args)
                first = f"{rows[0][0]} {rows[0][1]} p.{rows[0][2]}" if rows else "none"
                if s:
                    co, do, pa, capped = s
                    md.append(f"| {label} | {co:,} | {do:,} | {pa:,}{'+' if capped else ''} | {tc:.1f} | {len(rows)} | {tr:.1f} | {first} |")
                else:
                    md.append(f"| {label} | | | | | {len(rows)} | {tr:.1f} | {first} |")
            except Exception as e:
                conn.rollback()
                cur.execute("set statement_timeout = '120s'")
                bad += 1
                md.append(f"| {label} | failed: {type(e).__name__}: {str(e).splitlines()[0][:120]} | | | | | | |")
    md.append("")
    open("search_check.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("search_check.md", "w").write("### Search check: the script failed\n\n```\n" + traceback.format_exc()[-1500:] + "```\n")
        raise
