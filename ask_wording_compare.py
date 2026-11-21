#!/usr/bin/env python3
"""
ask_wording_compare.py: does option reader_plain_words change what the reader of Ask answers?

The option (off) changes three things in what the reader is told and nothing in what it reads: a confirmed fact is
"confirmed earlier by a person" (not "by a colleague"), the reporting year is named without WBA, and an answer is
never the bare word No or Yes. It changes the reader's instructions, so it is compared before it is switched on.
This script takes the newest answers of Ask (their question and the pages they read are kept in ask_log) and has the
database send each to the reader three times (public.ask_wording_trial): twice as today ("off" and "again": how much
two answers to the same pages differ by themselves) and once with the option ("on"). Nothing is kept.

It CALLS THE PAID READER: three calls a question (about 0.6 US cents a question at 8,000 characters a page). The owner
allowed it on 20 Nov 2026, up to about 0.6 USD. The log is public: it prints counts and shares only, never a question,
a company or a value.

    python ask_wording_compare.py [--limit 60]

Env: SUPABASE_DB_URL; AIRTABLE_QA_TOKEN for the run record (QA_COMMIT).
"""
import argparse, os, sys

import qa
from ask_names_compare import CASES_SQL, kept, share

ARMS = ("off", "again", "on")
FLAGS = ("bare", "starts_no", "not_stated", "colleague", "wba")


def summarise(trials):
    done = [t for t in trials if not t.get("skipped")]
    out = {"cases": len(done), "skipped": len(trials) - len(done), "tokens_in": sum(t.get("tokens_in", 0) for t in done),
           "tokens_out": sum(t.get("tokens_out", 0) for t in done), "with_known": sum(1 for t in done if t.get("known")),
           "with_fiscal": sum(1 for t in done if t.get("fiscal")), "arms": {}}
    for arm in ARMS:
        rows = [t[arm] for t in done]
        a = {"answered": sum(1 for r in rows if r.get("answered")), "facts": sum(r.get("facts", 0) for r in rows),
             "cites": sum(r.get("cites", 0) for r in rows), "length": sum(r.get("length", 0) for r in rows),
             "values": sum(len(r.get("values") or []) for r in rows),
             "values_kept": sum(kept(t["off"].get("values"), t[arm].get("values")) for t in done)}
        for f in FLAGS:
            a[f] = sum(1 for r in rows if r.get(f))
        out["arms"][arm] = a
    return out


def verdict(s):
    """(ok, reasons): the option may become the default when, against today's answers, it loses no more than 3 points
    of answers, of facts, or of the figures kept (measured against what a second plain answer keeps)."""
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
    lines = ["### Ask: plain words for the reader (option reader_plain_words)", "",
             f"{cases} questions answered three times from the pages they once read ({s['skipped']} skipped); {s['with_known']} had a confirmed fact "
             f"to show the reader, {s['with_fiscal']} a company whose year does not end in December. {s['tokens_in']:,} tokens read, {s['tokens_out']:,} written. Nothing was kept.", "",
             "| | as today | as today, again | with the option |", "|---|---|---|---|"]
    row = lambda label, f: lines.append(f"| {label} | {f(a['off'])} | {f(a['again'])} | {f(a['on'])} |")
    row("answered", lambda r: f"{r['answered']} of {cases}")
    row("facts with a page", lambda r: r["facts"])
    row("figures of the first answer kept", lambda r: f"{r['values_kept']} of {a['off']['values']} ({share(r['values_kept'], a['off']['values'])})")
    row("citations in the answers", lambda r: r["cites"])
    row("characters of answer", lambda r: f"{r['length']:,}")
    row("answers that are a bare No or Yes", lambda r: r["bare"])
    row("answers that begin with No", lambda r: r["starts_no"])
    row("answers that begin \"Not stated\"", lambda r: r["not_stated"])
    row("answers that say \"colleague\"", lambda r: r["colleague"])
    row("answers that say \"WBA\"", lambda r: r["wba"])
    lines += ["", "No attribute worse by more than 3 points: " + ("yes." if ok else "NO: " + "; ".join(worse) + "."), ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=60)
    a = ap.parse_args()
    import psycopg
    run = qa.Run("ask_wording_compare.py")
    trials = []
    with psycopg.connect(os.environ["SUPABASE_DB_URL"], autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute(CASES_SQL, (min(a.limit, 80),))               # 80 at most: the owner's ceiling of about 0.6 USD
        ids = [r[0] for r in cur.fetchall()]
        for n, i in enumerate(ids, 1):
            try:
                cur.execute("select public.ask_wording_trial(%s)", (i,))
                trials.append(cur.fetchone()[0])
            except Exception as e:                                # one question that fails does not cost the others
                trials.append({"skipped": type(e).__name__})
                run.failure("RUN_FAILED", f"ask_wording_trial: {type(e).__name__}", key=f"trial-{n}")
            print(f"  {n} of {len(ids)}" + (" skipped" if trials[-1].get("skipped") else ""), flush=True)
    s = summarise(trials)
    text = report(s)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    ok, _ = verdict(s)
    run.finish("ok", rows_in=len(ids), rows_out=s["cases"], notes=f"wording compare: {s['cases']} cases, not worse: {ok}",
               counters={"cases": s["cases"], "skipped": s["skipped"], "facts_off": s["arms"]["off"]["facts"], "facts_on": s["arms"]["on"]["facts"],
                         "bare_off": s["arms"]["off"]["bare"], "bare_on": s["arms"]["on"]["bare"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
