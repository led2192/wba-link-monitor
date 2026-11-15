#!/usr/bin/env python3
"""
ask_names_compare.py: does showing the reader the metric names in use change its answers, and does it line the names up?

Option ask_metric_names (off) gives the reader of Ask the names of the metrics closest to the question and tells it to
write a data point under a listed name when it is the same thing. It changes what the reader is told, so it is
compared before it is switched on. This script takes the newest answers of Ask (their question and the pages they
read are kept in ask_log) and has the database send each to the reader three times (public.ask_names_trial): twice
as today ("off" and "again": how much two answers to the same pages differ by themselves) and once with the names
("on"). Nothing is kept: no answer logged, no fact stored.

It CALLS THE PAID READER: three calls a question (about 0.3 US cents a question). The owner allowed it on 15 Nov 2026.
The log is public: it prints counts and shares only, never a question, a company, a name or a value.

What is compared, per answer and summed:
  answered        the reader wrote an answer
  facts           data points with a page behind them
  with a metric   facts whose name is a metric's name or one of its other names (the aim of the option)
  as listed       facts written letter for letter as a listed name
  same values     of the figures the "off" answer gave, how many the other answer gave too (against "again": the noise)
  same names      of the names the "off" answer used, how many the other answer used too

    python ask_names_compare.py [--limit 30]

Env: SUPABASE_DB_URL; AIRTABLE_QA_TOKEN for the run record (QA_COMMIT).
"""
import argparse, os, sys

import qa

CASES_SQL = """
select max(l.id) from public.ask_log l
where l.answered and jsonb_array_length(coalesce(l.sources, '[]'::jsonb)) > 0
group by lower(btrim(l.question)) order by 1 desc limit %s"""

ARMS = ("off", "again", "on")


def share(part, whole):
    return f"{100 * part / whole:.0f}%" if whole else "n/a"


def kept(base, other):
    """How many of base's items are in other too."""
    other = set(other or [])
    return sum(1 for x in (base or []) if x in other)


def summarise(trials):
    """Sums over the trials that ran: per arm the counts, and against "off" what the other two kept."""
    done = [t for t in trials if not t.get("skipped")]
    out = {"cases": len(done), "skipped": len(trials) - len(done), "tokens_in": sum(t.get("tokens_in", 0) for t in done),
           "tokens_out": sum(t.get("tokens_out", 0) for t in done), "arms": {}}
    for arm in ARMS:
        rows = [t[arm] for t in done]
        out["arms"][arm] = {
            "answered": sum(1 for r in rows if r.get("answered")), "facts": sum(r.get("facts", 0) for r in rows),
            "with_metric": sum(r.get("with_metric", 0) for r in rows), "as_listed": sum(r.get("as_listed", 0) for r in rows),
            "cites": sum(r.get("cites", 0) for r in rows), "length": sum(r.get("length", 0) for r in rows),
            "values": sum(len(r.get("values") or []) for r in rows), "names": sum(len(r.get("names") or []) for r in rows),
            "values_kept": sum(kept(t["off"].get("values"), t[arm].get("values")) for t in done),
            "names_kept": sum(kept(t["off"].get("names"), t[arm].get("names")) for t in done)}
    return out


def verdict(s):
    """(ok, lines): the option may become the default when, against the answers of today, it loses no more than 3
    points of answers, of facts an answer, or of the figures kept (measured against what a second plain answer keeps)."""
    a, cases = s["arms"], s["cases"]
    if not cases:
        return False, ["no case ran"]
    pts = lambda x, y: 100 * x / y if y else 0.0
    worse = []
    if pts(a["on"]["answered"], cases) < pts(a["off"]["answered"], cases) - 3:
        worse.append("fewer answers")
    if a["off"]["facts"] and a["on"]["facts"] < a["off"]["facts"] * 0.97 and a["on"]["facts"] < a["again"]["facts"] * 0.97:
        worse.append("fewer facts than both plain answers")
    base = a["off"]["values"]
    if base and pts(a["on"]["values_kept"], base) < pts(a["again"]["values_kept"], base) - 3:
        worse.append("keeps fewer of the figures than a second plain answer does")
    return not worse, worse


def report(s):
    a, cases = s["arms"], s["cases"]
    ok, worse = verdict(s)
    lines = ["### Ask: the metric names in use shown to the reader (option ask_metric_names)", "",
             f"{cases} questions answered three times from the pages they once read ({s['skipped']} skipped); "
             f"{s['tokens_in']:,} tokens read, {s['tokens_out']:,} written. Nothing was kept.", "",
             "| | as today | as today, again | with the names |", "|---|---|---|---|"]
    row = lambda label, f: lines.append(f"| {label} | {f(a['off'])} | {f(a['again'])} | {f(a['on'])} |")
    row("answered", lambda r: f"{r['answered']} of {cases}")
    row("facts with a page", lambda r: r["facts"])
    row("facts with a metric", lambda r: f"{r['with_metric']} ({share(r['with_metric'], r['facts'])})")
    row("facts named as listed", lambda r: f"{r['as_listed']} ({share(r['as_listed'], r['facts'])})")
    row("figures of the first answer kept", lambda r: f"{r['values_kept']} of {a['off']['values']} ({share(r['values_kept'], a['off']['values'])})")
    row("names of the first answer kept", lambda r: f"{r['names_kept']} of {a['off']['names']} ({share(r['names_kept'], a['off']['names'])})")
    row("citations in the answers", lambda r: r["cites"])
    row("characters of answer", lambda r: f"{r['length']:,}")
    lines += ["", "No attribute worse by more than 3 points: " + ("yes." if ok else "NO: " + "; ".join(worse) + "."), ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    a = ap.parse_args()
    import psycopg
    run = qa.Run("ask_names_compare.py")
    trials = []
    with psycopg.connect(os.environ["SUPABASE_DB_URL"], autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute(CASES_SQL, (a.limit,))
        ids = [r[0] for r in cur.fetchall()]
        for n, i in enumerate(ids, 1):
            try:
                cur.execute("select public.ask_names_trial(%s)", (i,))
                trials.append(cur.fetchone()[0])
            except Exception as e:                                   # one question that fails does not cost the others
                trials.append({"skipped": type(e).__name__})
                run.failure("RUN_FAILED", f"ask_names_trial: {type(e).__name__}", key=f"trial-{n}")
            print(f"  {n} of {len(ids)}" + (" skipped" if trials[-1].get("skipped") else ""), flush=True)
    s = summarise(trials)
    text = report(s)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    ok, _ = verdict(s)
    run.finish("ok", rows_in=len(ids), rows_out=s["cases"], notes=f"names compare: {s['cases']} cases, not worse: {ok}",
               counters={"cases": s["cases"], "skipped": s["skipped"], "with_metric_off": s["arms"]["off"]["with_metric"],
                         "with_metric_on": s["arms"]["on"]["with_metric"], "facts_off": s["arms"]["off"]["facts"], "facts_on": s["arms"]["on"]["facts"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
