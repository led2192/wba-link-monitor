#!/usr/bin/env python3
"""
search_check.py: read-only check of the evidence database's search functions (supabase/search.sql):
does each sample search answer, how many companies, documents and pages it finds, and how long it takes.
Prints counts, timings and the first result's company and page, never page text (the logs are public).

Run by the Search check workflow on pull requests that touch the search functions, and by hand.
Records its timings in the Pipeline QA runs table (counters), so a manual run can be read without the logs.
Env: SUPABASE_DB_URL; AIRTABLE_QA_TOKEN (optional; QA_COMMIT=true writes the run row).
"""
import os, sys, time, traceback

import qa

SEARCHES = [
    # (label, function, arguments)
    ("phrase: \"living wage\"", "pages", ('"living wage"',)),
    ("acronym: TNFD", "pages", ("TNFD",)),
    ("phrase: \"scope 3\"", "pages", ('"scope 3"',)),
    ("common word: climate", "pages", ("climate",)),
    ("very common word: revenue", "pages", ("revenue",)),
    ("documents view: revenue", "documents", ("revenue",)),
    ("documents view: \"living wage\"", "documents", ('"living wage"',)),
    ("two words, 2025 reports only: grievance mechanism", "pages", ("grievance mechanism", None, 2025, 2025)),
    ("company filter: \"net zero\" at Unilever", "pages", ('"net zero"', "Unilever")),
    ("other language: \"développement durable\"", "pages", ('"développement durable"',)),
    ("WBA: \"World Benchmarking Alliance\"", "pages", ('"World Benchmarking Alliance"',)),
    ("evidence passages, nature: water withdrawal", "passages", ("water withdrawal", "nature")),
    ("evidence passages, social, no words", "passages", ("", "social")),
    # the one-box search (find_pages, 10 Nov 2026): ranked by relevance; the owner's connection counts as a key
    ("find: climate", "find", ("climate",)),
    ("find: \"living wage\"", "find", ('"living wage"',)),
    ("find: Unilever scope 1, 2024", "find", ("scope 1", "Unilever", 2024, 2024)),
    ("find, little words: the scope 1 emissions of Nike in 2025", "find", ("the scope 1 emissions of Nike in 2025",)),
    ("find, relaxed: grievance mechanism remediation outcomes 2025 suppliers", "find", ("grievance mechanism remediation outcomes 2025 suppliers",)),
    ("find, synonym and industry: wba in Oil & Gas", "find_industry", ("wba", "Oil & Gas")),
    ("find, documents only: Nike code of conduct", "find", ("", "Nike", None, None, "Code of Conduct")),
    ("find, a quoted phrase alone: \"just transition\"", "find", ('"just transition"',)),
    ("find, neighbouring words: ACT Core World Benchmarking Alliance", "find", ("ACT Core World Benchmarking Alliance",)),
]


def has_function(cur, name):
    cur.execute("select to_regproc(%s) is not null", (f"public.{name}",))
    return cur.fetchone()[0]


def run(cur, fn, args, exact_companies=False):
    """(summary, rows, count time, results time, (exact companies, its time) or None). The exact company
    count (search_company_count, a probe per company) is asked when the summary was capped at 20,000
    pages, as the page does, and when the function is installed."""
    t0 = time.time()
    exact = None
    if fn == "pages":
        a = list(args) + [None] * (5 - len(args))
        cur.execute("select * from public.search_summary(%s, %s, %s, %s, %s)", a)
        summary = cur.fetchone()
        t1 = time.time()
        cur.execute("select company, year, page from public.search_pages(%s, %s, %s, %s, %s, 50)", a)
        rows = cur.fetchall()
        t2 = time.time()
        if exact_companies and summary and summary[3]:
            cur.execute("select public.search_company_count(%s, %s, %s, %s, %s)", a)
            exact = (cur.fetchone()[0], time.time() - t2)
        return summary, rows, t1 - t0, t2 - t1, exact
    if fn == "find":
        a = list(args) + [None] * (5 - len(args))
        cur.execute("select company, year, page from public.find_pages(null, %s, %s, %s, %s, %s, 50, 'search_check')", a)
        rows = cur.fetchall()
        return None, rows, 0.0, time.time() - t0, None
    if fn == "find_industry":
        cur.execute("select company, year, page from public.find_pages(null, %s, max_rows => 50, client => 'search_check', industry_filter => %s)", list(args))
        rows = cur.fetchall()
        return None, rows, 0.0, time.time() - t0, None
    if fn == "documents":
        a = list(args) + [None] * (5 - len(args))
        cur.execute("select company, year, pages from public.search_documents(%s, %s, %s, %s, %s, 50)", a)
        return None, cur.fetchall(), 0.0, time.time() - t0, None
    cur.execute("select company, year, page from public.search_passages(%s, %s, null, null, null, 50)", list(args))
    return None, cur.fetchall(), 0.0, time.time() - t0, None


def main():
    import psycopg
    qrun = qa.Run("search_check.py")
    timings = {}
    md = ["### Search check", "", "Read-only. Times are seconds from GitHub's servers to Frankfurt, network included.", "",
          "| Search | Companies | Documents | Pages | Count time | Results shown | Results time | First result |",
          "| --- | --: | --: | --: | --: | --: | --: | --- |"]
    # companies: the summary's figure, then "exact N (t s)" when the summary was capped and the function exists
    bad = 0
    with __import__("evidence_db").connect() as conn:
        cur = conn.cursor()
        cur.execute("set statement_timeout = '120s'")
        exact_ok = has_function(cur, "search_company_count")
        docs_ok = has_function(cur, "search_documents")
        find_ok = has_function(cur, "find_pages")
        if find_ok:                                           # the keys: at least one active, and a wrong key matches none
            try:
                cur.execute("select count(*) filter (where active), count(*) filter (where key_hash = encode(extensions.digest('not-a-key', 'sha256'), 'hex')) from public.search_keys")
                active, wrong = cur.fetchone()
                md.append(f"| find: keys | {active} active; a wrong key {'matches none' if wrong == 0 else 'MATCHES A KEY'} | | | | | | |")
                if has_function(cur, "find_terms"):
                    cur.execute("select phrases, words from public.find_terms(%s)", ("the scope 1 emissions of Nike in 2025 \"living wage\"",))
                    ph, wo = cur.fetchone()
                    md.append(f"| find: terms of 'the scope 1 emissions of Nike in 2025 \"living wage\"' | words {wo}, phrases {ph} | | | | | | |")
            except Exception as e:
                conn.rollback(); cur.execute("set statement_timeout = '120s'")
                md.append(f"| find: a wrong key | failed: {type(e).__name__} | | | | | | |")
        for label, fn, args in SEARCHES:
            if fn == "documents" and not docs_ok:
                continue
            if fn in ("find", "find_industry") and not find_ok:
                continue
            try:
                s, rows, tc, tr, exact = run(cur, fn, args, exact_ok)
                first = f"{rows[0][0]} {rows[0][1]} p.{rows[0][2]}" if rows else "none"
                timings[label] = {"rows": len(rows), "results_s": round(tr, 2), "count_s": round(tc, 2) if s else None,
                                  "companies": s[0] if s else None, "pages": s[2] if s else None}
                if exact:
                    timings[label].update({"exact_companies": exact[0], "exact_s": round(exact[1], 2)})
                if s:
                    co, do, pa, capped = s
                    co_txt = f"{co:,}{'+' if capped else ''}" + (
                        f", exact {exact[0]:,} ({exact[1]:.1f} s)" if exact and exact[0] >= 0 else
                        f", at least {-exact[0]:,} ({exact[1]:.1f} s, stopped)" if exact else "")
                    md.append(f"| {label} | {co_txt} | {do:,}{'+' if capped else ''} | {pa:,}{'+' if capped else ''} | {tc:.1f} | {len(rows)} | {tr:.1f} | {first} |")
                else:
                    md.append(f"| {label} | | | | | {len(rows)} | {tr:.1f} | {first} |")
            except Exception as e:
                conn.rollback()
                cur.execute("set statement_timeout = '120s'")
                bad += 1
                timings[label] = {"failed": f"{type(e).__name__}"}
                md.append(f"| {label} | failed: {type(e).__name__}: {str(e).splitlines()[0][:120]} | | | | | | |")
    md.append("")
    open("search_check.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    qrun.finish("failed" if bad else "ok", rows_in=len(SEARCHES), rows_out=len(SEARCHES) - bad, counters=timings)
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("search_check.md", "w").write("### Search check: the script failed\n\n```\n" + traceback.format_exc()[-1500:] + "```\n")
        raise
