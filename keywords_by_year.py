"""The words reports use most, by reporting year, and the ones rising or falling.

The owner asked (23 Nov 2026) for analysis across the documents for himself: "in the reports with 2025 data, which
keywords recur most?", and how a concept moves over the years. "Standards in use" counts the entries of the
reference list. This counts what is in no list: every pair of words that stand together in the main reports
("supply chain", "double materiality", "artificial intelligence"), by how many companies write it each year.

How: a random sample of the pages of main reports from FIRST_YEAR on (SAMPLE_PERCENT of them). Pages mostly in
English are kept (a page is English when a fifth of its words are among the commonest English words). Each page
is cut into sentences and each sentence into pairs of neighbouring content words (letters only, three or more,
not a common word). A pair is counted once a company a year. Kept: the KEEP pairs that most companies write each
year, and each year's number of companies sampled, so the page can show a share and compare years.

It ranks, it does not measure: the count comes from a sample, a few pages a company, so a share here is lower than
the true one and comparable only between years of the same run. No model, no cost.

Replaces the table in one transaction, only with --commit, and not in the hours people search (quiet_hours.py).
Prints counts only, never the words.
"""
import argparse, collections, os, re, sys

FIRST_YEAR = 2019
SAMPLE_PERCENT = 1.0
KEEP = 400
MIN_COMPANIES = 8
MAIN_TYPES = ("Annual Report", "Integrated Report", "Sustainability Report", "ESG Report", "10K Form", "Registration Document", "Impact Report")
COMMON = set("""the of and to in a is that for on with as by at from this be are or an it its our we was were has have had not but which their they
will can may all more also than other these those such into over under between during after before about been being would should could each any some
most per one two three four five six seven eight nine ten first second third new year years page report annual see note notes total including include
includes included further across within through where when while who whom whose what how there here both only very well however therefore thus due
based related following well non may million billion thousand table figure company companies group inc ltd plc december january february march april june
july august september october november fiscal ended ending respectively approximately continue continued continues part made make use used using key
high low higher lower around since up down out off no yes if then so do does did us you your he she his her them am i ii iii iv""".split())
ENGLISH = set("the of and to in a is that for on with as by at from this be are or an it we our was were has have not which their will can".split())
SAMPLE_SQL = """
select p.wba_id, p.year, p.body from public.pages p tablesample system (%s)
where p.year >= %s and p.year <= extract(year from now())::int and p.wba_id <> '' and p.doc_type = any(%s)"""
WORD = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)?")


def english(words):
    """True when the page reads as English: a fifth of its words or more are among the commonest."""
    return len(words) >= 40 and sum(1 for w in words if w in ENGLISH) / len(words) >= 0.2


def pairs(body):
    """The pairs of neighbouring content words of a page's sentences, lower case, each once."""
    out = set()
    for sentence in re.split(r"[.!?;:\n•|]+", body or ""):
        words = [w.lower() for w in WORD.findall(sentence)]
        for a, b in zip(words, words[1:]):
            if len(a) >= 3 and len(b) >= 3 and a not in COMMON and b not in COMMON and a != b:
                out.add(a + " " + b)
    return out


def count(rows, keep=KEEP, min_companies=MIN_COMPANIES):
    """({year: companies sampled}, [(year, phrase, companies)]) from (wba_id, year, body) rows: the pairs most companies write each year."""
    firms, seen = collections.defaultdict(set), collections.defaultdict(lambda: collections.defaultdict(set))
    for wba, year, body in rows:
        words = [w.lower() for w in WORD.findall(body or "")]
        if year is None or not english(words):
            continue
        firms[year].add(wba)
        for phrase in pairs(body):
            seen[year][phrase].add(wba)
    out = []
    for year, by in seen.items():
        top = sorted(((len(f), phrase) for phrase, f in by.items() if len(f) >= min_companies), key=lambda x: (-x[0], x[1]))[:keep]
        out += [(year, phrase, n) for n, phrase in top]
    # a phrase kept in one year is given its count in the other years too (even a small one), so the page can compare
    kept = {phrase for _, phrase, _ in out}; have = {(y, p) for y, p, _ in out}
    for year, by in seen.items():
        out += [(year, phrase, len(by[phrase])) for phrase in kept if (year, phrase) not in have and phrase in by]
    return {y: len(f) for y, f in firms.items()}, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--by-day", action="store_true", help="start a writing run in the hours people search too (quiet_hours.py)")
    ap.add_argument("--percent", type=float, default=SAMPLE_PERCENT)
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import quiet_hours
    wait = quiet_hours.held(a.commit, a.by_day)
    if wait:
        print(wait, flush=True)
        return 0
    import qa, evidence_db
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("keywords_by_year.py")
    with evidence_db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("set statement_timeout = '25min'")
            cur.execute(SAMPLE_SQL, (a.percent, FIRST_YEAR, list(MAIN_TYPES))); rows = cur.fetchall()
            firms, got = count(rows)
            print(f"{len(rows):,} pages sampled; {len(firms):,} years; {len({p for _, p, _ in got}):,} pairs of words kept, {len(got):,} year rows"
                  + ("" if a.commit else " (dry run: nothing written)"), flush=True)
            if a.commit and got:
                cur.execute("delete from public.keyword_use")
                cur.executemany("insert into public.keyword_use (year, phrase, companies, sampled) values (%s, %s, %s, %s)", [(y, p, n, firms[y]) for y, p, n in got])
                conn.commit()
            else:
                conn.rollback()
    run.finish("ok", rows_in=len(rows), rows_out=len(got) if a.commit else 0, counters={"pages": len(rows), "years": len(firms), "rows": len(got)}, notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
