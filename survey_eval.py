#!/usr/bin/env python3
"""
survey_eval.py: when a survey says yes, is it right, and how many of the analysts' yes does it find?

Why (16 Nov 2026). A survey (docs/evidence.md, "Surveys") asks one question of every company in a scope and counts
the yes. The first real one read 90 companies: 37 yes, 43 "not stated in the pages read". Before a colleague uses
such a count, two numbers are owed: of the answers analysts validated as yes, how many the survey finds; and of the
survey's yes, how many the analysts also say yes.

What it does. For yes/no attributes with validated answers in the gold bases (read-only): the two development
attributes that carry a question in gold_attributes.json, plus, with --auto N, the N development yes/no attributes
with most validated answers (their question is the description and definition of the bases' Metadata Codes), or,
with --heldout N, held-out attributes only (heldout.py: never tuned on; this script prints shares and counts and
nothing else of them, which is what the rule allows). For each answer the same reading a survey does is done:
public.survey_read (search.sql), the function survey_step itself calls, on the company's documents of the answer's
year and the next, with the words the survey would search with (public.survey_words, once an attribute) and the
question's vector (the same sentence model, when it can be loaded here).

An analyst's "No Evidence" is a no (the analyst looked and found nothing), as in narrative_eval.py. A survey's "not
stated" and "no page found" are not a no, and are counted apart: what matters is what the survey claims.

What it reports, per attribute code and in all, counts only:
  of the analysts' yes:  the survey's yes, no, not stated, no page, failed
  of the analysts' no:   the survey's yes (a claim the analyst does not share), no, not stated, no page, failed
  of the survey's yes:   with a page named, and with the copied sentence standing on that page
The two numbers to follow: yes found (survey yes over analysts' yes) and yes shared (analysts' yes over survey yes).
An analyst's no beside a survey's yes is not always the survey's mistake (the analyst may have read other
documents); the rows are not printed, so that share is an upper bound on its mistakes.

It calls the reader (a paid model, through the database, at about 0.0008 USD a reading) and stops at --max-usd.
Nothing is written to the database: every reading is rolled back, so the searches do not stay in the logs.

The log is public: attribute codes and counts only, never a company, a question, a sentence or a page.

    python survey_eval.py [--limit 30] [--auto 4 | --heldout 6] [--pages 6] [--options survey_strict,survey_again] [--max-usd 1.0] [--commit]

--options NAMES switches reading options on for this run only (lantern.NAME for the session; search.sql, survey_option).
The answers read are the same from run to run (pick), so a run with an option is compared with the run without it.
A reader does not answer twice alike: on 180 answers, a difference of a few answers is chance.

--commit records the run's counters in Pipeline QA (QA_COMMIT). Env: GOLD_TOKEN (read-only), SUPABASE_DB_URL.
"""
import argparse, collections, json, os, sys

import qa

HERE = os.path.dirname(os.path.abspath(__file__))
SAID = ("yes", "no", "not_stated", "no_page", "failed")
USD_IN, USD_OUT = 0.10 / 1e6, 0.50 / 1e6            # the reader's list price a token


def pick(answers, limit):
    """Up to `limit` answers of one attribute, yes and no in turn while both last (by id, so a run repeats)."""
    yes = sorted((g for g in answers if g["truth"]), key=lambda g: g["id"])
    no = sorted((g for g in answers if not g["truth"]), key=lambda g: g["id"])
    out = []
    while (yes or no) and (not limit or len(out) < limit):
        for side in (yes, no):
            if side and (not limit or len(out) < limit):
                out.append(side.pop(0))
    return out


def cost(stats):
    return stats["in"] * USD_IN + stats["out"] * USD_OUT


OPTIONS = ("survey_strict", "survey_again")


def read_one(cur, g, attr, pages, options=()):
    """One reading, as a survey does it: what public.survey_read returned (a dict), never kept in the database.
    The options are set before every reading: a setting is undone by the rollback that follows each one."""
    for name in OPTIONS:
        cur.execute("select set_config(%s, %s, false)", (f"lantern.{name}", "on" if name in options else "off"))
    cur.execute("select public.survey_read('eval', %s, %s, %s::real[], %s, %s, %s, %s, %s)",
                (attr["question"], attr["words"], attr.get("vec"), g["wba"], g["company"], int(g["year"]), int(g["year"]) + 1, pages))
    return cur.fetchone()[0] or {"status": "failed"}


def run_eval(cur, todo, attrs, pages, max_usd, out=print, options=()):
    """{code: Counter} over the answers. Keys: yes_<said>, no_<said> (analyst then survey), named, printed, in, out,
    failed. Stops reading once the cost reaches max_usd ("unread" counts what was left)."""
    stats = collections.defaultdict(collections.Counter)
    spent = 0.0
    for g in todo:
        s = stats[g["attribute"]]
        if spent >= max_usd:
            s["unread"] += 1
            continue
        try:
            r = read_one(cur, g, attrs[g["attribute"]], pages, options)
            cur.connection.rollback()
        except Exception as e:
            cur.connection.rollback()
            r = {"status": "failed"}
            out(f"  one answer not read ({type(e).__name__})")
        said = r.get("status") if r.get("status") in SAID else "failed"
        s[("yes_" if g["truth"] else "no_") + said] += 1
        if said == "yes":
            s["named"] += 1 if r.get("page") else 0
            s["printed"] += 1 if r.get("printed") else 0
            s["printed_shared"] += 1 if r.get("printed") and g["truth"] else 0
        s["in"] += int(r.get("in") or 0)
        s["out"] += int(r.get("out") or 0)
        spent += int(r.get("in") or 0) * USD_IN + int(r.get("out") or 0) * USD_OUT
    return stats


def total(stats):
    t = collections.Counter()
    for s in stats.values():
        t.update(s)
    return t


def share(a, b):
    return f"{a} of {b} ({100 * a / b:.0f}%)" if b else f"{a} of 0"


def side(s, truth):
    return sum(s[f"{truth}_{k}"] for k in SAID)


def table(stats, title):
    """The report as markdown lines: one row an attribute code, then all, then the two numbers to follow."""
    row = lambda name, s: (f"| {name} | {side(s, 'yes')} | {s['yes_yes']} | {s['yes_no']} | {s['yes_not_stated']} | {s['yes_no_page']} | "
                           f"{side(s, 'no')} | {s['no_yes']} | {s['no_no']} | {s['no_not_stated']} | {s['no_no_page']} | {s['yes_failed'] + s['no_failed']} |")
    md = [f"### {title}", "",
          "| Attribute | Analysts' yes | survey: yes | no | not stated | no page | Analysts' no | survey: yes | no | not stated | no page | Not read |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    md += [row(code, stats[code]) for code in sorted(stats)]
    t = total(stats)
    said_yes = t["yes_yes"] + t["no_yes"]
    md += [row("**All**", t), "",
           f"Yes found: of the analysts' yes, the survey says yes for {share(t['yes_yes'], side(t, 'yes'))}; it says no for {t['yes_no']} "
           f"and leaves {t['yes_not_stated'] + t['yes_no_page']} unanswered (not stated, or no page found).",
           f"Yes shared: of the survey's yes, the analysts also say yes for {share(t['yes_yes'], said_yes)}. "
           f"The other {t['no_yes']} are a yes where the analyst found no evidence: the survey's mistakes at most.",
           f"Of the analysts' no, the survey says no for {t['no_no']} and leaves {t['no_not_stated'] + t['no_no_page']} unanswered, of {side(t, 'no')}.",
           f"Of the survey's yes, a page is named for {share(t['named'], said_yes)} and the copied sentence stands on that page for {share(t['printed'], said_yes)}.",
           f"Counting only the yes whose sentence stands on its page: the analysts also say yes for {share(t['printed_shared'], t['printed'])}, "
           f"and {share(t['printed_shared'], side(t, 'yes'))} of the analysts' yes are found.",
           f"Read: {side(t, 'yes') + side(t, 'no')} answers, about {cost(t):.2f} USD ({t['in']:,} tokens in, {t['out']:,} out)"
           + (f"; {t['unread']} left unread at the ceiling of spending." if t["unread"] else ".")]
    return md


def embed(questions):
    """{question: vector as a list} with the sentence model of the index, or {} when it cannot be loaded here."""
    try:
        from semantic_index import Embedder
        e = Embedder()
        return {q: [float(x) for x in e.embed_query(q)] for q in questions}
    except Exception as e:
        print(f">>> the questions' vectors could not be made here ({type(e).__name__}): searched with the words alone", flush=True)
        return {}


def main(argv=None):
    ap = argparse.ArgumentParser(description="When a survey says yes, is it right, and how many of the analysts' yes does it find?")
    ap.add_argument("--limit", type=int, default=30, help="validated answers an attribute (0 = all)")
    ap.add_argument("--auto", type=int, default=0, help="also the N development yes/no attributes with most validated answers")
    ap.add_argument("--heldout", type=int, default=0, help="read only the N held-out yes/no attributes with most validated answers")
    ap.add_argument("--pages", type=int, default=6, help="pages read a company (a survey's setting; 1 to 8)")
    ap.add_argument("--options", default="", help="reading options to switch on for this run, comma separated: " + ", ".join(OPTIONS))
    ap.add_argument("--max-usd", type=float, default=1.0, help="stop reading at this cost")
    ap.add_argument("--commit", action="store_true", help="record the run's counters in Pipeline QA")
    a = ap.parse_args(argv)
    if not 1 <= a.pages <= 8 or a.limit < 0 or a.auto < 0 or a.heldout < 0 or not 0 < a.max_usd <= 5:
        sys.exit("--pages 1 to 8; --limit, --auto and --heldout 0 or more; --max-usd above 0 and at most 5.")
    options = tuple(o.strip() for o in a.options.split(",") if o.strip())
    if any(o not in OPTIONS for o in options):
        sys.exit("--options takes: " + ", ".join(OPTIONS))
    token = os.environ.get("GOLD_TOKEN", "").strip()
    if not token or not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set GOLD_TOKEN (read-only) and SUPABASE_DB_URL.")
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    import heldout, narrative_eval as N
    cfg_all = json.load(open(os.path.join(HERE, "gold_attributes.json"), encoding="utf-8"))
    bases = list(cfg_all["bases"].values())
    attrs = {c: {"question": v["question"]} for c, v in cfg_all["attributes"].items() if v.get("type") == "boolean" and v.get("question")}
    import psycopg
    log = qa.Run("survey_eval.py")
    try:
        if a.heldout:
            found = N.auto_attributes(token, bases, "reviewed", a.heldout * 3, set(), keep=heldout.is_heldout)
            attrs = {c: {"question": v["question"]} for c, v in found.items() if v["type"] == "boolean"}
            attrs = dict(list(attrs.items())[:a.heldout])
        elif a.auto:
            found = N.auto_attributes(token, bases, "reviewed", a.auto * 3, set(cfg_all["attributes"]), keep=lambda c: not heldout.is_heldout(c))
            attrs.update(dict([(c, {"question": v["question"]}) for c, v in found.items() if v["type"] == "boolean"][:a.auto]))
        answers = []
        for g in N.gold_answers(token, list(attrs), bases, "reviewed"):
            truth = N.truth_of(g["value"], g["null"], "boolean")
            if truth is not None and g.get("wba") and g.get("year"):
                answers.append(dict(g, truth=truth))
        by = collections.defaultdict(list)
        for g in answers:
            by[g["attribute"]].append(g)
        todo = [g for c in attrs for g in pick(by[c], a.limit)]
        print(f">>> {len(todo):,} validated yes/no answers to read, {len([c for c in attrs if by[c]])} attributes "
              f"({'held-out' if a.heldout else 'development'}), {a.pages} pages a company, at most {a.max_usd:.2f} USD", flush=True)
        if not todo:
            log.failure("RUN_FAILED", "survey_eval.py found no validated yes/no answers to read", key="survey_eval")
            log.finish(status="failed")
            return 1
        vectors = embed(sorted({v["question"][:400] for v in attrs.values()}))
        with psycopg.connect(os.environ["SUPABASE_DB_URL"]) as conn:
            cur = conn.cursor()
            cur.execute("set statement_timeout = '120s'")
            conn.commit()
            for c, v in attrs.items():
                v["question"] = v["question"][:400]
                cur.execute("select public.survey_words(%s)", (v["question"],))
                v["words"] = cur.fetchone()[0]
                v["vec"] = vectors.get(v["question"])
                conn.rollback()
            cur.execute("select wba_id, name from public.companies where wba_id = any(%s)", (sorted({g["wba"] for g in todo}),))
            names = dict(cur.fetchall())
            conn.rollback()
            for g in todo:
                g["company"] = names.get(g["wba"]) or g["wba"]
            stats = run_eval(cur, todo, attrs, a.pages, a.max_usd, options=options)
            conn.rollback()
        text = "\n".join(table(stats, f"Surveys against the analysts' validated yes/no answers ({'held-out' if a.heldout else 'development'} attributes, "
                                      f"{a.pages} pages a company, {'options ' + ', '.join(options) if options else 'no option'}, {'with' if vectors else 'without'} the question's vector)"))
        print(text)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8").write(text + "\n")
    except Exception as e:
        log.failure("RUN_FAILED", f"survey_eval.py stopped: {type(e).__name__}", key="survey_eval", message=str(e)[:300])
        log.finish(status="failed")
        raise
    t = total(stats)
    log.finish(status="ok", rows_in=side(t, "yes") + side(t, "no"), rows_out=0,
               counters={k: t[k] for k in ("yes_yes", "yes_no", "yes_not_stated", "yes_no_page", "no_yes", "no_no", "no_not_stated", "no_no_page", "named", "printed", "printed_shared", "in", "out")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
