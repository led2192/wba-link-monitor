#!/usr/bin/env python3
"""
search_exam.py: does the search find the pages that print the figures analysts validated?

Why (16 Nov 2026). ask_eval.py replays questions the search itself once answered: the cases replayed exactly find
their page in 13 of 13, so that set warns of a regression and cannot grade the search. This exam uses answers the
search had no part in: the validated answers of the gold bases (read-only) for the number attributes of
attributes.json (the development attributes; held-out attributes are never read here). For each answer, the search
is asked as a person would ask Ask: the attribute's name as the words, the company, the answer's year and the next
(a reporting year's document is often dated the year after). The eight pages an answer would read (ask_pages) and the
24 pages of the search (find_blend) are then checked for the validated number, printed at any of its scales.

What it reports, per attribute and in all, counts only:
  answers          validated answers with a value that can be looked for (a printed form of three or more digits)
  in the library   the number is printed on some page of the company's documents of those years (the ceiling:
                   what no search could exceed with today's documents)
  in the 24        on one of the 24 pages of the search
  in the eight     on one of the eight pages an answer reads
The number to follow is "in the eight" over "in the library".

A number can stand on a page by chance (a small figure, a busy table): forms of fewer than three significant digits
are not looked for, and the same check is used for the ceiling and for the search, so the share is fair even where
the counts flatter.

No model is called. The question's vector is used when the database already holds it (query_vector_made); otherwise
the words alone. Nothing is written to the database (the searches the run logs are rolled back).

The log is public: attribute codes and counts only, never a company, a value or a page.

    python search_exam.py [--limit 25] [--attributes GMC_00001.1,...] [--option NAME] [--commit]

--limit N answers an attribute (0 = all). --option NAME runs every answer with lantern.NAME off and then on.
--commit records the run's counters in Pipeline QA (QA_COMMIT). Env: GOLD_TOKEN (read-only), SUPABASE_DB_URL.
"""
import argparse, collections, json, os, re, sys

import qa
from value_eval import found_on, printed_forms, scales_for, tsquery_for

HERE = os.path.dirname(os.path.abspath(__file__))
MIN_DIGITS = 3            # a printed form of fewer significant digits is not looked for: it stands on pages by chance
KEYS = ("answers", "no_documents", "in_library", "in_search", "in_eight", "with_vector", "failed")


READER_CUTS = (3000, 8000)          # characters of a page the reader is given: today, and with the option reader_whole_page


def words_of(attr):
    """What a person would type for this attribute: its name, without punctuation."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s+&-]", " ", attr.get("name") or "")).strip()


def forms_of(value, attr):
    """The numbers this value can be printed as, at the scales of its unit family, of MIN_DIGITS digits or more."""
    return [(v, d) for v, digits, d in printed_forms(value, scales_for(attr.get("unit_family"))) if digits >= MIN_DIGITS]


def holds(bodies, forms):
    return any(found_on(b or "", v) for b in bodies for v, _ in forms)


def bodies_of(cur, keys):
    """The text of the pages 'library|page'."""
    if not keys:
        return []
    cur.execute("select p.body from public.pages p where p.library_id = any(%s) and p.library_id || '|' || p.page::text = any(%s)",
                (sorted({k.rsplit("|", 1)[0] for k in keys}), list(keys)))
    return [r[0] for r in cur.fetchall()]


def examine(cur, g, attr, option=None):
    """One answer: {'documents', 'in_library', 'in_search', 'in_eight', 'vector'} as booleans, or None when the value
    has no form to look for."""
    forms = forms_of(g["value"], attr)
    if not forms:
        return None
    r = dict(documents=False, in_library=False, in_search=False, in_eight=False, vector=False, **{f"in_view_{c}": False for c in READER_CUTS})
    y0, y1 = int(g["year"]), int(g["year"]) + 1
    if option:
        cur.execute("select set_config(%s, %s, false)", (f"lantern.{option[0]}", option[1]))
    cur.execute("select d.library_id from public.documents d where d.wba_id = %s and d.year between %s and %s", (g["wba"], y0, y1))
    ids = [x[0] for x in cur.fetchall()]
    if not ids:
        return r
    r["documents"] = True
    q = " | ".join(f"({tsquery_for(v, d)})" for v, d in forms)
    cur.execute("select body from public.pages where library_id = any(%s) and words @@ to_tsquery('simple', %s) limit 40", (ids, q))
    r["in_library"] = holds([x[0] for x in cur.fetchall()], forms)
    words = words_of(attr)
    cur.execute("select public.query_vector_made(%s)", (words,))
    vec = (cur.fetchone()[0] or {}).get("vec")
    r["vector"] = bool(vec)
    cur.execute("select library_id || '|' || page::text from public.ask_pages('eval', %s, %s::real[], %s, %s, %s, null, %s)",
                (words, vec, g["wba"], y0, y1, "exam"))
    eight = bodies_of(cur, [x[0] for x in cur.fetchall()])
    r["in_eight"] = holds(eight, forms)
    # What the reader is given of each of the eight pages: its first 3,000 characters, or 8,000 with the option
    # reader_whole_page (supabase/search.sql, reader_page_chars), spaces squeezed as the database squeezes them.
    for chars in READER_CUTS:
        r[f"in_view_{chars}"] = holds([re.sub(r"\s+", " ", b or "")[:chars] for b in eight], forms)
    cur.execute("select library_id || '|' || page::text from public.find_blend('eval', %s, %s::real[], %s, %s, %s, null, 24, 'eval', null, null, null, %s)",
                (words, vec, g["wba"], y0, y1, "exam"))
    r["in_search"] = r["in_eight"] or holds(bodies_of(cur, [x[0] for x in cur.fetchall()]), forms)
    return r


def run_exam(cur, todo, attrs, option=None, out=print):
    """{attribute code: Counter of KEYS} over the answers; a failed answer is counted and the run goes on."""
    stats = collections.defaultdict(collections.Counter)
    for g in todo:
        s = stats[g["attribute"]]
        try:
            r = examine(cur, g, attrs[g["attribute"]], option)
            cur.connection.rollback()                         # the searches of the exam are not kept in the logs
        except Exception as e:
            cur.connection.rollback()
            s["failed"] += 1
            out(f"  one answer not examined ({type(e).__name__})")
            continue
        if r is None:
            continue
        s["answers"] += 1
        s["no_documents"] += 0 if r["documents"] else 1
        s["in_library"] += 1 if r["in_library"] else 0
        s["in_search"] += 1 if r["in_library"] and r["in_search"] else 0
        s["in_eight"] += 1 if r["in_library"] and r["in_eight"] else 0
        for chars in READER_CUTS:
            s[f"in_view_{chars}"] += 1 if r["in_library"] and r["in_eight"] and r[f"in_view_{chars}"] else 0
        s["with_vector"] += 1 if r["vector"] else 0
    return stats


def total(stats):
    t = collections.Counter()
    for s in stats.values():
        t.update(s)
    return t


def share(a, b):
    return f"{a} of {b} ({100 * a / b:.0f}%)" if b else f"{a} of 0"


def table(stats, attrs, title):
    """The report as markdown lines: one row an attribute, then all."""
    md = [f"### {title}", "",
          "| Attribute | Answers | No document for the years | Number in the library | On the 24 pages of the search | On the eight pages an answer reads | In the first 3,000 characters of those pages (what the reader is given) | In the first 8,000 |",
          "|---|---|---|---|---|---|---|---|"]
    for code in sorted(stats):
        s = stats[code]
        md.append(f"| {code} {attrs[code].get('name', '')} | {s['answers']} | {s['no_documents']} | {s['in_library']} | {share(s['in_search'], s['in_library'])} | {share(s['in_eight'], s['in_library'])} | {share(s['in_view_3000'], s['in_eight'])} | {share(s['in_view_8000'], s['in_eight'])} |")
    t = total(stats)
    md.append(f"| **All** | {t['answers']} | {t['no_documents']} | {t['in_library']} | {share(t['in_search'], t['in_library'])} | {share(t['in_eight'], t['in_library'])} | {share(t['in_view_3000'], t['in_eight'])} | {share(t['in_view_8000'], t['in_eight'])} |")
    md += ["", f"Of the answers whose number is on the eight pages, the reader is given it in {share(t['in_view_3000'], t['in_eight'])} with pages cut at 3,000 characters and in "
               f"{share(t['in_view_8000'], t['in_eight'])} cut at 8,000 (option reader_whole_page): the difference is what the cut hides from the reader.",
           "", f"The search finds the validated number, where the library holds it: {share(t['in_eight'], t['in_library'])} on the eight pages an answer reads, "
               f"{share(t['in_search'], t['in_library'])} on the 24 pages of the search. The library holds it for {share(t['in_library'], t['answers'])} of the answers. "
               f"Searched with the question's vector too: {t['with_vector']}; not examined: {t['failed']}."]
    return md


def main(argv=None):
    ap = argparse.ArgumentParser(description="Does the search find the pages that print the validated figures?")
    ap.add_argument("--limit", type=int, default=25, help="answers an attribute (0 = all)")
    ap.add_argument("--attributes", default="", help="attribute codes, comma separated (default: every number attribute of attributes.json)")
    ap.add_argument("--option", default="", help="a search option (lantern.NAME) to run off and then on")
    ap.add_argument("--commit", action="store_true", help="record the run's counters in Pipeline QA")
    a = ap.parse_args(argv)
    if a.option and not re.fullmatch(r"[a-z_]{1,40}", a.option):
        sys.exit("--option takes the name of an option: small letters and underscores.")
    token = os.environ.get("GOLD_TOKEN", "").strip()
    if not token or not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set GOLD_TOKEN (read-only) and SUPABASE_DB_URL.")
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    spec = json.load(open(os.path.join(HERE, "attributes.json"), encoding="utf-8"))["attributes"]
    want = [c.strip() for c in a.attributes.split(",") if c.strip()]
    attrs = {c: v for c, v in spec.items() if v.get("type") == "number" and (not want or c in want)}
    if not attrs:
        sys.exit("No number attribute of attributes.json among those asked.")
    bases = list(json.load(open(os.path.join(HERE, "gold_attributes.json"), encoding="utf-8"))["bases"].values())
    import psycopg
    from value_eval import gold_values
    log = qa.Run("search_exam.py")
    try:
        answers = [g for b in bases for g in gold_values(token, b, list(attrs)) if g.get("value") and g.get("wba") and g.get("year")]
        by = collections.defaultdict(list)
        for g in answers:
            by[g["attribute"]].append(g)
        todo = [g for c in attrs for g in (by[c][:a.limit] if a.limit else by[c])]
        print(f">>> {len(todo):,} validated answers to examine, {len(attrs)} attributes", flush=True)
        if not todo:
            log.failure("RUN_FAILED", "search_exam.py found no validated answers to examine", key="search_exam")
            log.finish(status="failed")
            return 1
        md = []
        with psycopg.connect(os.environ["SUPABASE_DB_URL"]) as conn:
            cur = conn.cursor()
            cur.execute("set statement_timeout = '60s'")
            conn.commit()
            if a.option:
                off = run_exam(cur, todo, attrs, (a.option, "off"))
                stats = run_exam(cur, todo, attrs, (a.option, "on"))
                md += table(off, attrs, f"Search exam, option {a.option} off") + [""] + table(stats, attrs, f"Search exam, option {a.option} on")
            else:
                stats = run_exam(cur, todo, attrs)
                md += table(stats, attrs, "Search exam against the validated answers")
            conn.rollback()
        text = "\n".join(md)
        print(text)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8").write(text + "\n")
    except Exception as e:
        log.failure("RUN_FAILED", f"search_exam.py stopped: {type(e).__name__}", key="search_exam", message=str(e)[:300])
        log.finish(status="failed")
        raise
    t = total(stats)
    log.finish(status="ok", rows_in=t["answers"], rows_out=0, counters={k: t[k] for k in KEYS})
    return 0


if __name__ == "__main__":
    sys.exit(main())
