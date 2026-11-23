"""Examples for the terms of the reference list, taken from the reports themselves.

The owner asked (23 Nov 2026) for examples on the terms that gain from one. A glossary can say what double
materiality is; the library can show how three companies write about it, with the report and the page. This script
looks, for each term, for pages that print its name as a phrase, cuts the sentence that holds it (and the next one),
scores it, and keeps the best of up to EXAMPLES different companies. No model, no cost.

What makes a good example (score):
  - the sentence explains or applies the term: it holds a cue such as "is", "refers to", "means", "defined as",
    "we assess", "we use", "in line with" close to the name;
  - it reads as prose: 90 to 420 characters, mostly letters, no row of dots or of numbers (a contents page, a table,
    an index of a standard are left out);
  - the page is of a main report of a recent year.
An excerpt scored under MIN_SCORE is not kept: a term with no good example has none.

Rows a person rejected or approved in the admin panel are never touched; the others of the term are replaced. Writes
public.reference_examples only with --commit. Prints counts only, never names or excerpts.
"""
import argparse, os, re, sys, time

EXAMPLES = 3
FIRST_YEAR = 2022
PAGES_READ = 240
MIN_SCORE = 3
MAIN_TYPES = {"Annual Report", "Integrated Report", "Sustainability Report", "ESG Report", "Universal Registration Document", "Climate Report", "Impact Report"}
CUES = re.compile(r"\b(is|are|refers? to|means?|defined as|definition|describes?|we (?:assess|use|apply|define|consider|conduct|carried out|identif\w+|measure|report)|"
                  r"in line with|in accordance with|based on|approach to|process|principle|concept|so-called|known as|i\.e\.)\b", re.I)
ENTRIES_SQL = ("select e.id, e.name from public.reference_entries e left join public.reference_worked w on w.job = 'examples' and w.entry = e.id "
               "where e.kind = 'term' and not e.gone order by w.at nulls first, e.id")        # never looked for first, then the oldest
WORKED_SQL = "insert into public.reference_worked (job, entry) values (%s, %s) on conflict (job, entry) do update set at = now()"
PAGES_SQL = """
select p.library_id, p.page, p.wba_id, p.year, coalesce(p.doc_type, ''), p.body
from public.pages p
where p.words @@ phraseto_tsquery('simple', %s) and p.year >= %s and p.wba_id <> ''
limit %s"""


def sentences(body):
    """The page's text as sentences, spaces made single."""
    text = " ".join((body or "").split())
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(])", text) if s.strip()]


def prose(s):
    """True when the text reads as a sentence, not as a line of a table, an index or a list of numbers."""
    if not 90 <= len(s) <= 420 or "...." in s or " . . ." in s:
        return False
    letters = sum(c.isalpha() or c.isspace() for c in s)
    digits = sum(c.isdigit() for c in s)
    return letters / len(s) >= 0.86 and digits / len(s) <= 0.06 and s[0].isupper() and s.rstrip()[-1] in ".!?" and len(s.split()) >= 14


def excerpt_of(body, name):
    """(score, excerpt) for the best sentence of the page that holds the name, or None."""
    want = re.compile(r"(?<![A-Za-z])" + r"\s+".join(re.escape(w) for w in name.split()) + r"(?![A-Za-z])", re.I)
    ss, best = sentences(body), None
    for i, s in enumerate(ss):
        m = want.search(s)
        if not m or not prose(s):
            continue
        near = s[max(0, m.start() - 60):m.end() + 90]
        score = 1 + (2 if CUES.search(near) else 0) + (1 if m.start() < 60 else 0) + (1 if 140 <= len(s) <= 340 else 0)
        text = s
        if len(s) < 260 and i + 1 < len(ss) and prose(ss[i + 1]) and len(s) + len(ss[i + 1]) <= 440:
            text = s + " " + ss[i + 1]
        if best is None or score > best[0]:
            best = (score, text)
    return best


def pick(rows, name, examples=EXAMPLES, min_score=MIN_SCORE):
    """[(library_id, page, wba_id, year, excerpt, score)]: the best excerpt of each company, the best companies first."""
    by_company = {}
    for lib, page, wba, year, doc_type, body in rows:
        got = excerpt_of(body, name)
        if not got:
            continue
        score = got[0] + (1 if doc_type in MAIN_TYPES else 0)
        if score < min_score:
            continue
        key = (score, year or 0, -len(got[1]))
        if wba not in by_company or key > by_company[wba][0]:
            by_company[wba] = (key, (lib, page, wba, year, got[1], score))
    return [v for _, v in sorted(by_company.values(), key=lambda x: x[0], reverse=True)[:examples]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="terms at most (0: all)")
    ap.add_argument("--minutes", type=float, default=100)
    ap.add_argument("--seconds-a-term", type=int, default=60)
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import psycopg, qa, evidence_db
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("reference_examples.py")
    began, done, with_one, kept, slow = time.time(), 0, 0, 0, 0
    with evidence_db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(ENTRIES_SQL); entries = [e for e in cur.fetchall() if len(e[1].strip()) >= 4]
            conn.commit()
            if a.limit:
                entries = entries[:a.limit]
            print(f"{len(entries):,} terms to look for", flush=True)
            for eid, name in entries:
                if time.time() - began > a.minutes * 60:
                    break
                try:
                    cur.execute("select set_config('statement_timeout', %s, true)", (f"{a.seconds_a_term}s",))
                    cur.execute(PAGES_SQL, (name, FIRST_YEAR, PAGES_READ)); got = pick(cur.fetchall(), name)
                except psycopg.errors.QueryCanceled:
                    conn.rollback(); slow += 1
                    if a.commit:
                        cur.execute(WORKED_SQL, ("examples", eid)); conn.commit()
                    continue
                if a.commit:
                    cur.execute("delete from public.reference_examples where entry = %s and status = 'found'", (eid,))
                    cur.executemany("insert into public.reference_examples (entry, library_id, page, wba_id, year, excerpt, score) values (%s, %s, %s, %s, %s, %s, %s) "
                                    "on conflict (entry, library_id, page) do nothing", [(eid,) + g for g in got])
                    cur.execute(WORKED_SQL, ("examples", eid))
                    conn.commit()
                else:
                    conn.rollback()
                done += 1; with_one += 1 if got else 0; kept += len(got)
                if done % 100 == 0:
                    print(f"  {done:,} terms, {with_one:,} with an example, {time.time() - began:,.0f} s", flush=True)
    left = len(entries) - done - slow
    print(f"{done:,} terms looked for: {with_one:,} have an example ({kept:,} excerpts); {slow:,} too slow and skipped; {left:,} left for the next run"
          + ("" if a.commit else " (dry run: nothing written)"), flush=True)
    if slow:
        run.failure("DATA_CHECK", f"{slow} terms were not searched within {a.seconds_a_term} s", key=qa.key("DATA_CHECK", "reference_examples_slow"), severity="Low")
    run.finish("ok", rows_in=len(entries), rows_out=kept if a.commit else 0, counters={"terms": done, "with_example": with_one, "excerpts": kept, "slow": slow, "left": left},
               notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
