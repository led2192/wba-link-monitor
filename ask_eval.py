#!/usr/bin/env python3
"""
ask_eval.py: do the pages an answer reads still include the pages people confirmed?

The set (13 Nov 2026). Every answer of Ask leaves its facts in public.ask_facts with the page each was read on, and
people mark facts right. A question with at least one fact marked right is a real question with a known right page.
This script takes those questions (the newest 300) and asks the search, as Ask does today (ask_pages: the words the
question was searched with and the question's vector, for the fact's company), for the eight pages an answer would
read. It reports for how many questions a confirmed page is among them, with the words and the meaning together and
with the words alone, and at which place. Nothing here calls a model: the words are the ones kept in ask_log, the
vector comes from query_vector (kept in query_vectors once asked).

What it is good for: a change to the search or to the choice of pages (ask_pages) is measured on the same questions
before and after. What it is not: the confirmed pages are pages an answer once read, so the set leans to what the
search already found; a page nobody was shown cannot be in it. The search is narrowed to the fact's company, which the
question as typed may not have been. It grows as people confirm facts.

The log is public: it prints counts and shares only, never a question, a company or a page.

    python ask_eval.py [--limit 300] [--commit]

--commit writes one row to public.ask_eval (the run's counts). Env: SUPABASE_DB_URL; AIRTABLE_QA_TOKEN for the run
record (QA_COMMIT is set by --commit).
"""
import argparse, os, sys

import qa

CASES_SQL = """
select a.id, coalesce(nullif(btrim(a.keywords), ''), a.question) as q, a.question, f.wba_id,
       array_agg(distinct f.library_id || '|' || f.page::text) as right_pages
from public.ask_log a join public.ask_facts f on f.ask_id = a.id
where a.answered and f.verdict = 'right' and f.library_id is not null and f.page is not null and f.wba_id is not null
group by a.id, a.keywords, a.question, f.wba_id
order by a.id desc limit %s"""

TABLE_SQL = """
create table if not exists public.ask_eval (
  id bigint generated always as identity primary key, at timestamptz not null default now(),
  cases int not null, found int not null, found_words int not null, first_three int not null, no_vector int not null, commit_sha text);
alter table public.ask_eval enable row level security"""


def place(pages, right):
    """1-based place of the first confirmed page among the pages an answer would read; 0 when none is there."""
    for i, p in enumerate(pages, 1):
        if p in right:
            return i
    return 0


def evaluate(cur, cases, out=print):
    """Counts over the cases: found (words and meaning), found_words (words alone), first_three, no_vector."""
    c = dict(cases=0, found=0, found_words=0, first_three=0, no_vector=0, failed=0)
    for ask_id, q, question, wba, right_pages in cases:
        right = set(right_pages or [])
        try:
            cur.execute("select public.query_vector_made(%s)", (question,))
            vec = (cur.fetchone()[0] or {}).get("vec")
            both = pages_for(cur, q, vec, wba, ask_id)
            words = pages_for(cur, q, None, wba, ask_id) if vec else both
        except Exception as e:
            cur.connection.rollback(); c["failed"] += 1
            out(f"  one case not run ({type(e).__name__})")
            continue
        c["cases"] += 1
        c["no_vector"] += 0 if vec else 1
        at = place(both, right)
        c["found"] += 1 if at else 0
        c["first_three"] += 1 if 0 < at <= 3 else 0
        c["found_words"] += 1 if place(words, right) else 0
    return c


def pages_for(cur, q, vec, wba, ask_id):
    cur.execute("select library_id || '|' || page::text from public.ask_pages('eval', %s, %s::real[], %s, null, null, null, %s)",
                (q, vec, wba, f"eval {ask_id}"))
    return [r[0] for r in cur.fetchall()]


def summary(c):
    n = c["cases"] or 1
    pct = lambda k: f"{c[k]} of {c['cases']} ({100 * c[k] / n:.0f}%)"
    return (f"{c['cases']} questions with a confirmed page. A confirmed page is among the eight pages an answer reads: {pct('found')}; "
            f"among the first three: {pct('first_three')}; with the words alone: {pct('found_words')}. "
            f"Questions with no vector: {c['no_vector']}; not run: {c['failed']}.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Do the pages an answer reads still include the pages people confirmed?")
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args(argv)
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    import psycopg
    log = qa.Run("ask_eval.py")
    try:
        with psycopg.connect(os.environ["SUPABASE_DB_URL"]) as conn:
            cur = conn.cursor()
            cur.execute("set statement_timeout = '60s'")
            cur.execute(CASES_SQL, (a.limit,))
            cases = cur.fetchall()
            c = evaluate(cur, cases)
            conn.rollback()                                   # the searches logged by the run are not kept
            print(summary(c))
            if a.commit:
                for stmt in TABLE_SQL.split(";"):
                    cur.execute(stmt)
                cur.execute("insert into public.ask_eval (cases, found, found_words, first_three, no_vector, commit_sha) values (%s, %s, %s, %s, %s, %s)",
                            (c["cases"], c["found"], c["found_words"], c["first_three"], c["no_vector"], (os.environ.get("GITHUB_SHA") or "")[:12]))
                conn.commit()
    except Exception as e:
        log.failure("RUN_FAILED", f"ask_eval.py stopped: {type(e).__name__}", key="ask_eval", message=str(e)[:300])
        log.finish(status="failed")
        raise
    log.finish(status="ok", rows_in=c["cases"], rows_out=1 if a.commit else 0, counters=c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
