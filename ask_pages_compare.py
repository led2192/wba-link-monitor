#!/usr/bin/env python3
"""
ask_pages_compare.py: the pages an answer would read, with an option off and on. No model is called: nothing is paid.

Ask reads eight pages (public.ask_pages). A change to which eight is a change to a ranker, so it ships as an
option and is looked at beside the default first (the rule of the house). This script asks ask_pages twice for
each case, with lantern.<option> off and on in the session, and prints for each case: the seconds, the years
of the eight pages both ways, how many pages are the same, and the newest year among the company's main
reports (what a question with no year is about).

The cases are a fixed list of (words, company): questions with no year about a company whose reports the
library holds for several years. The vector is not used (words only), so the semantic half of the blend is not
compared here.

With --option ask_later the same cases are asked about a year: the year two before the company's newest main report
(a figure of that year is printed again, sometimes restated, by the two later reports). The count compared is then
the pages of reports later than the year asked, and the report also says how many pages of the year asked stay.

    python ask_pages_compare.py [--option ask_latest | ask_later]

Run by hand through the Search check workflow (mode = ask_pages). Env: SUPABASE_DB_URL. Prints years, counts and
seconds, never page text.
"""
import argparse, os, re, sys, time

CASES = [
    ("scope 3 categories report", "Nike"),
    ("scope 1 emissions", "Nike"),
    ("emissions reduction target", "ANA Holdings"),
    ("living wage suppliers", "Unilever"),
    ("scope 3 emissions", "Nestle"),
    ("net zero target", "Shell"),
    ("human rights due diligence", "Inditex"),
    ("water withdrawal", "Coca-Cola"),
    ("renewable electricity share", "Iberdrola"),
    ("gender pay gap", "BBVA"),
    ("deforestation commitment", "Danone"),
    ("board independence", "Toyota"),
]
MAIN = ("Annual Report", "Integrated Report", "Sustainability Report", "ESG Report", "CSR Report", "Impact Report", "Climate Report", "10K Form", "Registration Document")


def years(rows):
    """'2022 x6, 2025 x2' from [(library_id, page, year)], newest first."""
    count = {}
    for _, _, y in rows:
        count[y] = count.get(y, 0) + 1
    return ", ".join(f"{y or 'no year'} x{n}" for y, n in sorted(count.items(), key=lambda kv: -(kv[0] or 0))) or "none"


def recent(rows, latest):
    """How many of the pages are of the newest main report's year or the year before."""
    return sum(1 for _, _, y in rows if latest and y and y >= latest - 1)


def year_asked(option, latest):
    """The year a case is asked about: none, or with ask_later the year two before the newest main report."""
    return latest - 2 if option == "ask_later" and latest else None


def counted(option, rows, latest):
    """What the option is meant to raise: pages of the newest two years, or with ask_later pages later than the year asked."""
    if option == "ask_later":
        return sum(1 for _, _, y in rows if latest and y and y > latest - 2)
    return recent(rows, latest)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--option", default="ask_latest")
    a = ap.parse_args()
    if not re.fullmatch(r"[a-z_]+", a.option):
        sys.exit("option: letters and underscores")
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import evidence_db
    md = [f"### Ask pages compare: option `{a.option}` beside the default", "",
          "| case | newest main report | s off | s on | years off | years on | counted off | counted on | same of 8 |", "|---|---|---|---|---|---|---|---|---|"]
    better = worse = 0
    with evidence_db.connect() as conn:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("set statement_timeout = '90s'")
        for words, company in CASES:
            label = f"{company} · {words}"
            try:
                cur.execute("select max(d.year) from public.documents d where d.wba_id = any(public.find_company_ids(%s)) and coalesce(d.pages, 0) > 2 and d.doc_type = any(%s)", (company, list(MAIN)))
                latest = cur.fetchone()[0]
                asked = year_asked(a.option, latest)
                got = {}
                for on in (False, True):
                    cur.execute("select set_config(%s, %s, false)", (f"lantern.{a.option}", "on" if on else "off"))
                    t0 = time.time()
                    cur.execute("select library_id, page, year from public.ask_pages('', %s, null, %s, %s, %s, null, %s)", (words, company, asked, asked, words))
                    got[on] = (cur.fetchall(), time.time() - t0)
            except Exception as e:                                   # a timeout is a result, not a crash
                md.append(f"| {label} | failed: {type(e).__name__} | | | | | | | |")
                print(f">>> {label}: {type(e).__name__}", flush=True)
                continue
            (off, s_off), (on, s_on) = got[False], got[True]
            same = len({(r[0], r[1]) for r in off} & {(r[0], r[1]) for r in on})
            r_off, r_on = counted(a.option, off, latest), counted(a.option, on, latest)
            if asked:
                label += f" · {asked} ({sum(1 for _, _, y in on if y == asked)} of its pages stay)"
            better += r_on > r_off; worse += r_on < r_off
            md.append(f"| {label} | {latest or 'none'} | {s_off:.1f} | {s_on:.1f} | {years(off)} | {years(on)} | {r_off} | {r_on} | {same} |")
            print(f">>> {label}: newest main report {latest}; {s_off:.1f} s -> {s_on:.1f} s; years {years(off)} -> {years(on)}; recent {r_off} -> {r_on}; same {same}", flush=True)
    what = "of reports later than the year asked" if a.option == "ask_later" else "of the newest two years"
    md += ["", f"Cases with more pages {what}: {better}; with fewer: {worse}; of {len(CASES)}."]
    print(md[-1], flush=True)
    open("search_check.md", "w").write("\n".join(md) + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        open(summary, "a").write("\n".join(md) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
