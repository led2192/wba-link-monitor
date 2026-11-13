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
search already found; a page nobody was shown cannot be in it. An answer given from 13 Nov 2026 keeps what it was
searched with (ask_log.search) and is asked again exactly so; an older one kept only the question, which is searched as
typed within the fact's company (a guess: the box had read the company and the years out of it). The report says how
many are of each kind, and whether a confirmed page is at least among the 24 pages of the search (then the choice of
eight lost it, not the search). It grows as people confirm facts.

The log is public: it prints counts and shares only, never a question, a company or a page.

    python ask_eval.py [--limit 300] [--commit]

--commit writes one row to public.ask_eval (the run's counts). Env: SUPABASE_DB_URL; AIRTABLE_QA_TOKEN for the run
record (QA_COMMIT is set by --commit).
"""
import argparse, os, sys

import qa

CASES_SQL = """
select a.id, a.keywords, a.search, a.question, f.wba_id,
       array_agg(distinct f.library_id || '|' || f.page::text) as right_pages
from public.ask_log a join public.ask_facts f on f.ask_id = a.id
where a.answered and f.verdict = 'right' and f.library_id is not null and f.page is not null and f.wba_id is not null
group by a.id, a.keywords, a.search, a.question, f.wba_id
order by a.id desc limit %s"""

TABLE_SQL = """
create table if not exists public.ask_eval (
  id bigint generated always as identity primary key, at timestamptz not null default now(),
  cases int not null, found int not null, found_words int not null, first_three int not null, no_vector int not null, commit_sha text);
alter table public.ask_eval add column if not exists exact int;
alter table public.ask_eval add column if not exists exact_found int;
alter table public.ask_eval add column if not exists in_search int;
alter table public.ask_eval enable row level security"""


def place(pages, right):
    """1-based place of the first confirmed page among the pages an answer would read; 0 when none is there."""
    for i, p in enumerate(pages, 1):
        if p in right:
            return i
    return 0


def replay(keywords, search, question, wba):
    """(the words to try in turn, company, year from, year to, type, exact). With the search Ask kept (ask_log.search,
    from 13 Nov 2026) the same search is asked again: the model's words first, then the words the box sent, with the
    box's filters. Older answers kept only the question: it is searched as typed, within the fact's company."""
    tries = [w for w in [(keywords or "").strip()] if w]
    if search:
        return tries + [search.get("q") or question], search.get("company"), search.get("from"), search.get("to"), search.get("type"), True
    return tries + [question], wba, None, None, None, False


def evaluate(cur, cases, out=print):
    """Counts over the cases: found (words and meaning), found_words (words alone), first_three, no_vector, in_search
    (a confirmed page is among the 24 pages of the search, whether or not it is one of the eight read), and exact /
    exact_found for the cases asked again exactly as they were."""
    c = dict(cases=0, found=0, found_words=0, first_three=0, no_vector=0, failed=0, exact=0, exact_found=0, in_search=0)
    for ask_id, keywords, search, question, wba, right_pages in cases:
        right = set(right_pages or [])
        tries, company, y0, y1, kind, exact = replay(keywords, search, question, wba)
        try:
            cur.execute("select public.query_vector_made(%s)", (question,))
            vec = (cur.fetchone()[0] or {}).get("vec")
            both = pages_for(cur, tries, vec, company, y0, y1, kind, ask_id)
            words = pages_for(cur, tries, None, company, y0, y1, kind, ask_id) if vec else both
            wide = pages_for(cur, tries, vec, company, y0, y1, kind, ask_id, search=True)
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
        c["in_search"] += 1 if place(wide, right) else 0
        c["exact"] += 1 if exact else 0
        c["exact_found"] += 1 if exact and at else 0
    return c


def pages_for(cur, tries, vec, company, y0, y1, kind, ask_id, search=False):
    """The pages of the first words that find any: the eight an answer reads (ask_pages), or with search=True the 24
    pages of the search they are chosen from (find_blend)."""
    for q in tries:
        if search:
            cur.execute("select library_id || '|' || page::text from public.find_blend('eval', %s, %s::real[], %s, %s, %s, %s, 24, 'eval', null, null, null, %s)",
                        (q, vec, company, y0, y1, kind, f"eval {ask_id}"))
        else:
            cur.execute("select library_id || '|' || page::text from public.ask_pages('eval', %s, %s::real[], %s, %s, %s, %s, %s)",
                        (q, vec, company, y0, y1, kind, f"eval {ask_id}"))
        got = [r[0] for r in cur.fetchall()]
        if got:
            return got
    return []


def summary(c):
    n = c["cases"] or 1
    pct = lambda k: f"{c[k]} of {c['cases']} ({100 * c[k] / n:.0f}%)"
    return (f"{c['cases']} questions with a confirmed page. A confirmed page is among the eight pages an answer reads: {pct('found')}; "
            f"among the first three: {pct('first_three')}; with the words alone: {pct('found_words')}; among the 24 pages of the search: {pct('in_search')}. "
            f"Asked again exactly as they were (the search was kept): {c['exact']}, found {c['exact_found']}; the others are searched as typed, within the fact's company. "
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
                cur.execute("insert into public.ask_eval (cases, found, found_words, first_three, no_vector, commit_sha, exact, exact_found, in_search) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                            (c["cases"], c["found"], c["found_words"], c["first_three"], c["no_vector"], (os.environ.get("GITHUB_SHA") or "")[:12], c["exact"], c["exact_found"], c["in_search"]))
                conn.commit()
    except Exception as e:
        log.failure("RUN_FAILED", f"ask_eval.py stopped: {type(e).__name__}", key="ask_eval", message=str(e)[:300])
        log.finish(status="failed")
        raise
    log.finish(status="ok", rows_in=c["cases"], rows_out=1 if a.commit else 0, counters=c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
