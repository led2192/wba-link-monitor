#!/usr/bin/env python3
"""
search_compare.py: a search option beside the default, on the same searches. Writes nothing but the search log.

The rule of the house: a change to how pages are found ships as an option, is run next to the default on the
same questions, and becomes the default only when nothing gets worse. This script runs find_pages
(supabase/search.sql) twice for each search, with the option off and on (set lantern.<option> in the session,
the owner's connection), and reports for each:

  seconds        the whole search, as the function logs it
  pages          rows returned (of 50 asked)
  together       of the first ten, the pages whose excerpt marks the words as one piece or side by side
  same           of the first ten with the option on, how many were in the first ten without it

and, from the click log (search_clicks: the pages people opened, or marked useful, after a search of theirs):

  clicked found  of the pages opened in past searches, how many come back in the first ten, with and without
  MRR            the mean of 1 / rank of the opened page (0 when it is not in the first fifty)

The searches are a fixed list (the slow and the wrong ones of 10 and 11 Nov 2026) and, with --clicks, the past
searches that ended in a click (the most recent N distinct ones). It prints counts, ranks and seconds, never
page text and never what people typed in the click log (the logs are public): those searches are numbered.

    python search_compare.py [--option pairs] [--clicks 40]

Run by hand through the Search check workflow (input mode = compare). Env: SUPABASE_DB_URL.
It asks the database about 2 x (len(CASES) + clicks) searches: the live search is slower while it runs.
"""
import argparse, os, re, sys, time

CASES = [
    # (text, company filter or None, year or None)
    ("living wage", None, None),
    ("just transition plan", None, None),
    ("grievance mechanism", None, None),
    ('"human rights due diligence"', None, None),
    ('"ACT Core"', None, None),
    ('"just transition"', None, None),
    ("scope 3 emissions target", None, None),
    ("deforestation free supply chain", None, None),
    ("board gender diversity", None, None),
    ("climate", None, None),
    ("living wage", "Nike", None),
    ("scope 1", "Unilever", 2024),
]
MARK = re.compile(r"⟦([^⟧]*)⟧(\s*)(?=⟦)|⟦[^⟧]*\s[^⟧]*⟧")      # two marks side by side, or one mark of several words


def together(excerpt):
    return bool(MARK.search(excerpt or ""))


def run(cur, option, on, text, company, year, client):
    """[(library_id, page, excerpt)] in rank order, and the seconds the function logged."""
    cur.execute("select set_config(%s, %s, false)", (f"lantern.{option}", "on" if on else "off"))
    t0 = time.time()
    cur.execute("select library_id, page, excerpt, log_id from public.find_pages(%s, %s, %s, %s, %s, null, 50, %s)",
                ("", text, company, year, year, client))
    rows = cur.fetchall()
    secs = time.time() - t0
    return [(r[0], r[1], r[2]) for r in rows], secs


def summary(rows):
    return {"n": len(rows), "together": sum(together(r[2]) for r in rows[:10]), "top": [(r[0], r[1]) for r in rows[:10]]}


def rank_of(rows, lid, page):
    for i, r in enumerate(rows, 1):
        if r[0] == lid and r[1] == page:
            return i
    return 0


def click_stats(ranks):
    """ranks: the rank of each opened page (0 = not in the first fifty) -> (found in ten, MRR)."""
    if not ranks:
        return 0, 0.0
    return sum(1 for r in ranks if 0 < r <= 10), sum(1.0 / r for r in ranks if r) / len(ranks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--option", default="pairs")
    ap.add_argument("--clicks", type=int, default=40, help="past searches that ended in a click to replay (0 = none)")
    a = ap.parse_args()
    if not re.fullmatch(r"[a-z_]+", a.option):
        sys.exit("option: letters and underscores")
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import evidence_db
    md = [f"### Search compare: option `{a.option}` beside the default", "",
          "| search | s off | s on | pages off | pages on | together off | together on | same of 10 |", "|---|---|---|---|---|---|---|---|"]
    tot = {"off": 0.0, "on": 0.0}
    with evidence_db.connect() as conn:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("set statement_timeout = '60s'")
        for text, company, year in CASES:
            label = " · ".join(str(x) for x in (company, text, year) if x)
            try:
                off, s_off = run(cur, a.option, False, text, company, year, "compare off")
                on, s_on = run(cur, a.option, True, text, company, year, "compare on")
            except Exception as e:                                   # a timeout is a result, not a crash
                md.append(f"| {label} | failed: {type(e).__name__} | | | | | | |")
                print(f">>> {label}: {type(e).__name__}", flush=True)
                continue
            so, sn = summary(off), summary(on)
            same = len(set(so["top"]) & set(sn["top"]))
            tot["off"] += s_off; tot["on"] += s_on
            md.append(f"| {label} | {s_off:.1f} | {s_on:.1f} | {so['n']} | {sn['n']} | {so['together']} | {sn['together']} | {same} |")
            print(f">>> {label}: {s_off:.1f} s -> {s_on:.1f} s; pages {so['n']} -> {sn['n']}; together {so['together']} -> {sn['together']}; same {same}", flush=True)
        md += ["", f"Seconds in all: {tot['off']:.1f} off, {tot['on']:.1f} on."]
        if a.clicks:
            cur.execute("""
                select distinct on (l.q, l.company, l.year_from, l.year_to, l.doc_type) l.q, l.company, l.year_from, l.year_to, c.library_id, c.page
                from public.search_clicks c join public.search_log l on l.id = c.log_id
                where coalesce(l.q, '') <> '' and coalesce(l.client, '') not like 'compare%%' and c.library_id is not null and c.kind in ('open', 'useful')
                order by l.q, l.company, l.year_from, l.year_to, l.doc_type, c.at desc limit %s""", (a.clicks,))
            clicks = cur.fetchall()
            r_off, r_on = [], []
            for i, (q, company, y0, y1, lid, page) in enumerate(clicks, 1):
                try:
                    off, _ = run(cur, a.option, False, q, company, y0 if y0 == y1 else None, "compare off")
                    on, _ = run(cur, a.option, True, q, company, y0 if y0 == y1 else None, "compare on")
                except Exception as e:
                    print(f">>> click {i}: {type(e).__name__}", flush=True)
                    continue
                r_off.append(rank_of(off, lid, page)); r_on.append(rank_of(on, lid, page))
                print(f">>> click {i}: rank {r_off[-1]} -> {r_on[-1]}", flush=True)
            f_off, m_off = click_stats(r_off); f_on, m_on = click_stats(r_on)
            md += ["", f"Click log: {len(r_off)} past searches that ended in a click, replayed. Opened page back in the first ten: "
                       f"{f_off} without the option, {f_on} with it. MRR {m_off:.3f} without, {m_on:.3f} with. "
                       f"(Few clicks so far: read this as a check that nothing breaks, not as a measure.)"]
        cur.execute("select set_config(%s, 'off', false)", (f"lantern.{a.option}",))
    text = "\n".join(md) + "\n"
    open("search_check.md", "w", encoding="utf-8").write(text)
    print(text)
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        open(summ, "a", encoding="utf-8").write(text)


if __name__ == "__main__":
    main()
