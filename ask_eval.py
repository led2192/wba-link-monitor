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

    python ask_eval.py [--limit 300] [--commit] [--option ask_later] [--why]

--commit writes one row to public.ask_eval (the run's counts).
--option NAME runs the same questions twice in one session, with the option off and then on (set_config of
  lantern.NAME, set again before every question because a failed question rolls the session's settings back), and
  reports both lines and how many questions gained or lost their confirmed page. It writes nothing. (15 Nov 2026: the
  option had been passed through PGOPTIONS, which this connection does not carry, so two runs compared nothing.)
--why adds, for the questions whose confirmed page is not among the 24 pages of the search, a count by cause: the
  document is there with another page, the search's own filters leave the document out, the page is found deeper
  (places 25 to 200), or it is not found at all (and whether the document is indexed by meaning). It ends with how
  many of the questions whose confirmed page is not read have their confirmed figure printed on a page that is read
  (a figure often stands on several pages of a report). Counts only. Env: SUPABASE_DB_URL; AIRTABLE_QA_TOKEN for the run
record (QA_COMMIT is set by --commit).
"""
import argparse, os, re, sys

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


def evaluate(cur, cases, out=print, option=None, places=None, missed=None):
    """Counts over the cases: found (words and meaning), found_words (words alone), first_three, no_vector, in_search
    (a confirmed page is among the 24 pages of the search, whether or not it is one of the eight read), and exact /
    exact_found for the cases asked again exactly as they were. option = (name, 'on' or 'off') sets lantern.name for
    the session before every case. places, when given, receives {case number: (place among the eight, in the search,
    the eight pages)} (the case's number in the list, since one question about two companies is two cases);
    missed receives what --why needs of each case whose confirmed page is not among the 24. found_doc counts the cases
    where a confirmed page's document is among the eight with any page (a figure often stands on several pages of a
    report, so this is the kinder reading); with_years those whose search named years."""
    c = dict(cases=0, found=0, found_words=0, first_three=0, no_vector=0, failed=0, exact=0, exact_found=0, in_search=0, found_doc=0, with_years=0)
    for number, (ask_id, keywords, search, question, wba, right_pages) in enumerate(cases):
        right = set(right_pages or [])
        tries, company, y0, y1, kind, exact = replay(keywords, search, question, wba)
        try:
            if option:
                cur.execute("select set_config(%s, %s, false)", (f"lantern.{option[0]}", option[1]))
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
        c["found_doc"] += 1 if {doc_of(k) for k in both} & {doc_of(k) for k in right} else 0
        c["with_years"] += 1 if y0 is not None or y1 is not None else 0
        if places is not None:
            places[number] = (at, bool(place(wide, right)), tuple(both))
        if missed is not None and not place(wide, right):
            missed.append((ask_id, tries, vec, company, y0, y1, kind, right, wide))
    return c


CAUSES = ("the document is among the 24 pages, with other pages of it",
          "the document is no longer in the database",
          "the search's years leave the document out",
          "the search's type leaves the document out",
          "the search's company leaves the document out",
          "the page is found deeper, between places 25 and 200",
          "the document is found deeper, with other pages of it",
          "not found among 200 pages; the document is indexed by meaning",
          "not found among 200 pages; the document is not indexed by meaning",
          "not looked at (the database failed)")


def doc_of(key):
    return key.rsplit("|", 1)[0]


def apart(right, wide):
    """The fewest pages between a confirmed page and a page of the same document among the pages found; None when
    the document is not among them."""
    gaps = [abs(int(k.rsplit("|", 1)[1]) - int(w.rsplit("|", 1)[1])) for k in right for w in wide if doc_of(k) == doc_of(w)]
    return min(gaps) if gaps else None


def at_the_cap(right, wide, cap=3):
    """True when a confirmed page's document has as many pages among the pages found as the search allows a document."""
    docs = {doc_of(k) for k in right}
    return any(sum(1 for w in wide if doc_of(w) == d) >= cap for d in docs)


def cause(cur, case):
    """Why no confirmed page of this case is among the 24 pages of its search: the first of CAUSES that holds for any
    of its confirmed pages, in that order (the mildest first)."""
    ask_id, tries, vec, company, y0, y1, kind, right, wide = case
    docs = sorted({doc_of(k) for k in right})
    if {doc_of(k) for k in wide} & set(docs):
        return CAUSES[0]
    cur.execute("select d.library_id, d.year, d.doc_type, d.wba_id, exists (select 1 from public.chunk_docs c where c.library_id = d.library_id) "
                "from public.documents d where d.library_id = any(%s)", (docs,))
    rows = cur.fetchall()
    if not rows:
        return CAUSES[1]
    ids = None
    if company:
        cur.execute("select public.find_company_ids(%s)", (company,))
        ids = cur.fetchone()[0]
    by_year = lambda r: (y0 is not None and (r[1] is None or r[1] < y0)) or (y1 is not None and (r[1] is None or r[1] > y1))
    by_type = lambda r: bool(kind) and (r[2] or "") != kind
    by_company = lambda r: ids is not None and r[3] not in ids
    inside = [r for r in rows if not (by_year(r) or by_type(r) or by_company(r))]
    if not inside:
        return CAUSES[2] if all(by_year(r) for r in rows) else CAUSES[3] if all(by_year(r) or by_type(r) for r in rows) else CAUSES[4]
    deep = []
    for q in tries:
        cur.execute("select library_id || '|' || page::text from public.find_blend('eval', %s, %s::real[], %s, %s, %s, %s, 200, 'eval', null, null, null, %s)",
                    (q, vec, company, y0, y1, kind, f"eval {ask_id}"))
        deep = [r[0] for r in cur.fetchall()]
        if deep:
            break
    if place(deep, right):
        return CAUSES[5]
    if {doc_of(k) for k in deep} & set(docs):
        return CAUSES[6]
    return CAUSES[7] if any(r[4] for r in inside) else CAUSES[8]


def why(cur, missed, out=print):
    """A count by cause over the missed cases, as lines; nothing of a case is printed."""
    n = {}
    for case in missed:
        try:
            got = cause(cur, case)
        except Exception as e:
            cur.connection.rollback()
            out(f"  one case not looked at ({type(e).__name__})")
            got = CAUSES[-1]
        n[got] = n.get(got, 0) + 1
    lines = [f"Confirmed page not among the 24 pages of the search: {len(missed)} questions."] + [f"  {n[k]}: {k}" for k in CAUSES if k in n]
    gaps = [g for g in (apart(m[7], m[8]) for m in missed) if g is not None]
    if gaps:
        lines.append(f"  Where the document is there with other pages, the nearest page found is the next or the one before in "
                     f"{sum(1 for g in gaps if g == 1)}, two or three pages away in {sum(1 for g in gaps if 2 <= g <= 3)}, "
                     f"four to ten in {sum(1 for g in gaps if 4 <= g <= 10)}, further in {sum(1 for g in gaps if g > 10)}.")
        full = sum(1 for m in missed if at_the_cap(m[7], m[8]))
        lines.append(f"  The search gives at most three pages a document: the confirmed page's document already has its three among the 24 in {full} of these {len(gaps)}.")
    return lines


def digits(value):
    """The significant digits of a stored figure, as they would be printed whatever the scale: '390000.00' -> '39',
    '12345.60' -> '123456'. Stored values are the printed number times the page's scale, so the zeros at the end say
    nothing about what the page prints."""
    t = str(value).strip().lstrip("-")
    if "." in t:
        t = t.rstrip("0")
    return re.sub(r"\D", "", t).lstrip("0").rstrip("0")


def printed(body):
    """A page's text with the separators inside numbers taken out, so 1,234.5 and 1 234,5 both read 12345."""
    return re.sub(r"(?<=\d)[,.'\u2019\u00a0\u202f ](?=\d)", "", body or "")


def figures_read(cur, cases, places, out=print):
    """Of the cases whose confirmed page is not among the eight read: how many have a confirmed figure (a number of at
    least four significant digits) and, of those, in how many that figure is printed on one of the eight pages read.
    A count of how far 'the confirmed page is not read' overstates 'the figure is not read'. One line, counts only."""
    lost = [(n, cases[n]) for n in sorted(places) if not places[n][0]]
    with_figure = on_a_page = 0
    for n, (ask_id, _k, _s, _q, wba, _r) in lost:
        try:
            cur.execute("select f.value::text from public.ask_facts f where f.ask_id = %s and f.wba_id = %s and f.verdict = 'right' and f.kind = 'number' and f.value is not null", (ask_id, wba))
            want = {d for d in (digits(r[0]) for r in cur.fetchall()) if len(d) >= 4}
            if not want:
                continue
            with_figure += 1
            keys = list(places[n][2])
            if not keys:
                continue
            cur.execute("select p.body from public.pages p where p.library_id || '|' || p.page::text = any(%s) and p.library_id = any(%s)",
                        (keys, sorted({doc_of(k) for k in keys})))
            text = " ".join(printed(r[0]) for r in cur.fetchall())
            on_a_page += 1 if any(d in text for d in want) else 0
        except Exception as e:
            cur.connection.rollback()
            out(f"  one case not looked at ({type(e).__name__})")
    return (f"Confirmed page not among the eight read: {len(lost)} questions. With a confirmed figure of four or more significant digits: {with_figure}; "
            f"of those, the figure is printed on one of the eight pages read in {on_a_page}.")


def compare(off, on):
    """One line: the questions that gained or lost their confirmed page with the option on, among the eight and in the search."""
    both = [k for k in off if k in on]
    g8 = sum(1 for k in both if on[k][0] and not off[k][0]); l8 = sum(1 for k in both if off[k][0] and not on[k][0])
    gs = sum(1 for k in both if on[k][1] and not off[k][1]); ls = sum(1 for k in both if off[k][1] and not on[k][1])
    moved = sum(1 for k in both if on[k][0] and off[k][0] and on[k][0] != off[k][0])
    changed = sum(1 for k in both if len(on[k]) > 2 and len(off[k]) > 2 and on[k][2] != off[k][2])
    return (f"With the option on, of {len(both)} questions run both ways: the eight pages changed in {changed}; among the eight, {g8} gained and {l8} lost their confirmed page "
            f"({moved} kept it at another place); among the 24 of the search, {gs} gained and {ls} lost."
            + (" The option changed no page of any question: it did not act on this set (or was not set)." if changed == 0 else ""))


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
            f"The confirmed page's document is among the eight with any of its pages: {pct('found_doc')}. Searches that named years: {c.get('with_years', 0)}. "
            f"Asked again exactly as they were (the search was kept): {c['exact']}, found {c['exact_found']}; the others are searched as typed, within the fact's company. "
            f"Questions with no vector: {c['no_vector']}; not run: {c['failed']}.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Do the pages an answer reads still include the pages people confirmed?")
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--option", default="", help="a search option (lantern.NAME) to run off and then on; writes nothing")
    ap.add_argument("--why", action="store_true", help="count the causes of the questions whose confirmed page the search does not find")
    a = ap.parse_args(argv)
    if a.option and not re.fullmatch(r"[a-z_]{1,40}", a.option):
        sys.exit("--option takes the name of an option: small letters and underscores.")
    if a.option and a.commit:
        sys.exit("--option compares and writes nothing: leave --commit out.")
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
            missed = [] if a.why else None
            if a.option:
                off, on = {}, {}
                c_off = evaluate(cur, cases, option=(a.option, "off"), places=off)
                conn.rollback()
                c = evaluate(cur, cases, option=(a.option, "on"), places=on, missed=missed)
                conn.rollback()
                print(f"Option {a.option} off. " + summary(c_off))
                print(f"Option {a.option} on. " + summary(c))
                print(compare(off, on))
            else:
                on = {} if a.why else None
                c = evaluate(cur, cases, missed=missed, places=on)
                conn.rollback()                               # the searches logged by the run are not kept
                print(summary(c))
            if a.why:
                for line in why(cur, missed):
                    print(line)
                print(figures_read(cur, cases, on))
                conn.rollback()
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
