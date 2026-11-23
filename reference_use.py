"""How much each norm and regulation of the reference list is used in the reports, by year.

The owner asked (23 Nov 2026) which standards the reports of a year name most. The reference list holds the names
and acronyms of norms, frameworks and regulations; the library holds the words of every page. This script counts,
for each entry and each reporting year, the documents and the companies with at least one page that prints the
entry's name, or one of its acronyms, as a phrase. No model, no cost: one indexed search an entry.

Rules:
  - Kinds counted: norm, regulation and term (the owner, 23 Nov 2026: "it is important to see how a concept moves over
    the years"). Hidden entries are left out.
  - Documents counted: those of a known kind (annual and sustainability reports, presentations, policies and the
    rest of the list of types); a document typed UNKNOWN or not typed is left out.
  - A name or acronym shorter than 3 characters is not searched, nor an acronym that is an everyday word of the
    library (it appears on more than COMMON_SHARE of all pages' vocabulary count): "ACT" or "CARE" would count every
    page that says act or care. The count is then by the full name only.
  - An acronym is counted only when the pages that hold the word mostly write it as the acronym: of a sample of 200
    such pages, 70% or more print it with its capitals. "EDGE", "STAR", "ART" are norms' acronyms and everyday words,
    and the word index does not know capitals. Such an entry is counted by its full name only.
  - Pages are matched whole-word, whatever the case. An acronym shared with something else is counted too: the
    numbers say "names it", not "applies it".
  - Counts replace the entry's earlier counts (delete and insert in one transaction an entry). An entry whose search
    runs past the time limit is skipped and counted in the run's line.

Writes public.reference_use only with --commit. Prints counts only, never names.
"""
import argparse, os, re, sys, time

FIRST_YEAR = 2018
KINDS = ("norm", "regulation", "term")
MIN_CHARS = 3
COMMON_WORD_N = 200000          # a single-word acronym the library's vocabulary counts on more pages than this is an everyday word
ENTRIES_SQL = ("select e.id, e.name, e.aliases from public.reference_entries e left join public.reference_worked w on w.job = 'use' and w.entry = e.id "
               "where e.kind = any(%s) and not e.gone order by w.at nulls first, (e.kind = 'term'), e.id desc")  # never counted first (from the end of the list: the first runs stopped before it), then the oldest count
WORKED_SQL = "insert into public.reference_worked (job, entry) values (%s, %s) on conflict (job, entry) do update set at = now()"
COUNT_SQL = """
with q as (select (select string_agg('(' || phraseto_tsquery('simple', t)::text || ')', ' | ') from unnest(%s::text[]) t
                   where phraseto_tsquery('simple', t)::text <> '')::tsquery as tsq)
-- pages carries its document's year and company (kept in step by a trigger), so no join is needed
select p.year, count(distinct p.library_id), count(distinct nullif(p.wba_id, ''))
from public.pages p, q
where q.tsq is not null and p.words @@ q.tsq and p.year >= %s and p.year <= extract(year from now())::int + 1
  and coalesce(p.doc_type, '') not in ('', 'UNKNOWN')        -- documents of a known kind only (reports, presentations, policies...), not what nobody could type
group by 1"""


def phrases(name, aliases, common):
    """The texts searched for one entry: its name, and the acronyms that are not too short and not an everyday word."""
    out = []
    for i, t in enumerate([name] + list(aliases or [])):
        t = " ".join(str(t or "").split())
        if len(t) < MIN_CHARS or t.lower() in {x.lower() for x in out}:
            continue
        if i and " " not in t and t.lower() in common:
            continue
        out.append(t)
    return out


WRITTEN_SQL = """
select count(*) filter (where s.body ~ %s), count(*)
from (select p.body from public.pages p where p.words @@ plainto_tsquery('simple', %s) limit %s) s"""
SAMPLE = 200
AS_WRITTEN = 0.7


def is_acronym(text):
    """One word with two capitals or more ("TCFD", "SBTi", "EDGE"): the index does not know capitals, so it is checked."""
    return " " not in text and sum(ch.isupper() for ch in text) >= 2


def written_share(cur, acronym, sample=SAMPLE):
    """The share of a sample of the pages that hold the word where it is written as the acronym, capitals and all.
    "EDGE", "STAR", "ART" or "TREES" are acronyms of norms and everyday words: most pages that hold the word write
    "edge" or "star", and counting them made those norms the most used of all (23 Nov 2026). 1.0 when no page holds it."""
    cur.execute(WRITTEN_SQL, (r"\m" + re.escape(acronym) + r"\M", acronym, sample))
    hit, seen = cur.fetchone()
    return 1.0 if not seen else hit / seen


def checked(cur, texts, floor=AS_WRITTEN):
    """The texts without the acronyms that the pages mostly write as an everyday word."""
    return [t for t in texts if not is_acronym(t) or written_share(cur, t) >= floor]


def count_entry(cur, texts, first_year=FIRST_YEAR):
    """[(year, documents, companies)] for the texts, or [] when none can be searched."""
    if not texts:
        return []
    cur.execute(COUNT_SQL, (texts, first_year))
    return [(int(y), int(n), int(c)) for y, n, c in cur.fetchall() if y is not None]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="entries at most (0: all)")
    ap.add_argument("--minutes", type=float, default=100, help="stop cleanly after this many minutes")
    ap.add_argument("--seconds-an-entry", type=int, default=90)
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import psycopg, qa, evidence_db
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("reference_use.py")
    began, done, rows, slow, empty = time.time(), 0, 0, 0, 0
    with evidence_db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(ENTRIES_SQL, (list(KINDS),)); entries = cur.fetchall()
            cur.execute("select word from public.search_vocab where n > %s", (COMMON_WORD_N,)); common = {r[0] for r in cur.fetchall()}
            conn.commit()
            if a.limit:
                entries = entries[:a.limit]
            print(f"{len(entries):,} entries to count; {len(common):,} everyday words are not searched as acronyms", flush=True)
            for eid, name, aliases in entries:
                if time.time() - began > a.minutes * 60:
                    break
                try:
                    cur.execute("select set_config('statement_timeout', %s, true)", (f"{a.seconds_an_entry}s",))
                    got = count_entry(cur, checked(cur, phrases(name, aliases, common)))
                except psycopg.errors.QueryCanceled:
                    conn.rollback(); slow += 1
                    if a.commit:                                      # marked too: a slow entry must not be the first of every run
                        cur.execute(WORKED_SQL, ("use", eid)); conn.commit()
                    continue
                if a.commit:
                    cur.execute("delete from public.reference_use where entry = %s", (eid,))
                    cur.executemany("insert into public.reference_use (entry, year, documents, companies) values (%s, %s, %s, %s)", [(eid, y, n, c) for y, n, c in got])
                    cur.execute(WORKED_SQL, ("use", eid))
                    conn.commit()
                else:
                    conn.rollback()
                done += 1; rows += len(got); empty += 0 if got else 1
                if done % 100 == 0:
                    print(f"  {done:,} entries counted, {time.time() - began:,.0f} s", flush=True)
    left = len(entries) - done - slow
    print(f"{done:,} entries counted ({empty:,} named by no document), {rows:,} entry-years; {slow:,} too slow and skipped; {left:,} left for the next run"
          + ("" if a.commit else " (dry run: nothing written)"), flush=True)
    if slow:
        run.failure("DATA_CHECK", f"{slow} reference entries were not counted within {a.seconds_an_entry} s", key=qa.key("DATA_CHECK", "reference_use_slow"), severity="Low")
    run.finish("ok", rows_in=len(entries), rows_out=rows if a.commit else 0, counters={"counted": done, "none": empty, "slow": slow, "left": left},
               notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
