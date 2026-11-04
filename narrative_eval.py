#!/usr/bin/env python3
"""
narrative_eval.py: can the model answer a judgement attribute from the company's own pages?

The first narrative attributes (3 Nov 2026): governance of sustainability, validated by the Climate team at
attribute level in ACT Core Climate. Each reviewed answer is either a value (true, or a list of options) with
the analyst's evidence, or "No Evidence": the analyst looked and found nothing, which is an answer too ("no").
For every answer this script:
  1. takes the company's documents of the reporting year in the evidence database (sustainability, annual,
     integrated, ESG and climate reports first), and the pages that best match the attribute's search words;
  2. asks the model the attribute's question on those pages (one call, a few pages), with a quote and a page;
  3. scores the reply against the analyst: yes/no right, and for a list the exact set and each option.
"Non-English Evidence" and "Insufficient Evidence" answers are left out: the first are not readable here yet,
the second are neither yes nor no.

The attributes, their question and options live in gold_attributes.json (entries with "question"). Read-only
against the gold base (GOLD_TOKEN), the evidence database (SUPABASE_DB_URL); calls the model
(ANTHROPIC_API_KEY; READER_MODEL, default the reader's Haiku). Writes narrative_eval.md; with --commit, the run
and one scoreboard row per attribute in Pipeline QA. Dry run by default: --limit answers per attribute (15).
"""
import argparse, collections, json, os, re, sys, time, traceback

import qa
from import_source_library import get_all

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "gold_attributes.json")
GOLD_BASE = "appWuFWIlbffP9vCY"          # ACT Core Climate: validated at attribute level
DOC_TYPES = ["Sustainability Report", "Integrated Report", "Annual Report", "ESG Report", "Climate Report",
             "CSR Report", "Registration Document", "10K Form", "CDP Report"]
MAX_DOCS, PAGES, PAGE_CHARS = 6, 6, 2500
LEFT_OUT = {"Non-English Evidence", "Insufficient Evidence"}
SYSTEM = ("You read pages of a company's own reports and answer one question about what the company discloses. "
          "Answer only from the pages given. Reply with one JSON object only: no code fence, no text around it.")


def gold_id(mid):
    m = re.match(r"^(PT_\d{5})-(GMC_\d{5}\.\d+)-(\d{4})$", (mid or "").strip())
    return (m.group(1), m.group(2), int(m.group(3))) if m else (None, None, None)


def truth_of(value, null_reason, kind):
    """The analyst's answer: True/False for yes/no, a set of options for a list; None to leave the row out."""
    if (null_reason or "") in LEFT_OUT:
        return None
    if null_reason == "No Evidence" and not (value or "").strip():
        return False if kind == "boolean" else frozenset()
    v = (value or "").strip()
    if kind == "boolean":
        return v.lower() in ("true", "yes", "1") if v else None
    return frozenset(o.strip() for o in v.split(",") if o.strip()) if v else None


def gold_answers(token, codes):
    formula = "AND({Data Reviewed}, OR(" + ",".join(f"FIND('-{c}-', {{Metadata ID}})" for c in codes) + "))"
    rows = get_all(token, GOLD_BASE, "Metadata", ["Metadata ID", "Selected Value", "Null Value Reasoning", "Evidence", "Page Number"],
                   formula)
    out = []
    for r in rows:
        f = r["fields"]
        wba, code, year = gold_id(f.get("Metadata ID"))
        if code in codes:
            nr = f.get("Null Value Reasoning")
            out.append({"id": f.get("Metadata ID"), "wba": wba, "attribute": code, "year": year,
                        "value": f.get("Selected Value") or "", "null": nr.get("name") if isinstance(nr, dict) else (nr or ""),
                        "evidence": f.get("Evidence") or "", "pages": f.get("Page Number") or ""})
    return out


def or_query(queries):
    """to_tsquery text: any of the queries, each with all its words (extract_eval.words_query: no stopwords,
    no word order, which the word index checks fast)."""
    from extract_eval import words_query
    return words_query(queries)


def company_pages(cur, wba, year, queries, fy_month=None):
    """[(library_id, doc_type, page, body)]: the best pages for the queries in the company's documents of the
    reporting year (the year after it when the fiscal year ends January to June) or the next."""
    from extract_eval import document_year
    want = document_year(year, fy_month)
    cur.execute("select library_id, coalesce(doc_type, ''), coalesce(year, 0) from public.documents "
                "where wba_id = %s and year in (%s, %s)", (wba, want, want + 1))
    rank = {t: i for i, t in enumerate(DOC_TYPES)}
    docs = sorted(cur.fetchall(), key=lambda d: (rank.get(d[1], 99), d[2] != want, d[0]))[:MAX_DOCS]
    if not docs:
        return []
    types = {d[0]: d[1] for d in docs}
    q = or_query(queries)
    cur.execute("select library_id, page, body from public.pages, to_tsquery('simple', %s) q "
                "where library_id = any(%s) and words @@ q order by ts_rank_cd(words, q, 32) desc limit %s",
                (q, list(types), PAGES))
    return [(lid, types.get(lid, ""), page, body or "") for lid, page, body in cur.fetchall()]


def prompt(attr, pages, company):
    kind = attr["type"]
    lines = [f"Question: {attr['question']}"]
    if kind == "boolean":
        lines.append('Answer "yes" only when a page states it; "no" when the pages do not.')
        shape = '{"answer": "yes" or "no", "page": the page number or null, "quote": "at most 25 words copied from the page", "why": "ten words"}'
    else:
        opts = attr["options"]
        lines.append("Options: " + "; ".join(opts) + ". Pick every option the pages show, none if they show none.")
        shape = '{"answer": [the options shown], "page": the page number or null, "quote": "at most 25 words copied from the page", "why": "ten words"}'
    lines += ["Pages:", ""]
    for lid, dtype, page, body in pages:
        lines += [f"<page document=\"{dtype or 'report'}\" number=\"{page}\">", body[:PAGE_CHARS], "</page>", ""]
    lines.append("Reply: " + shape)
    return "\n".join(lines)


ANSWER = re.compile(r'"answer"\s*:\s*("(?:yes|no|true|false)"|\[[^\]]*\])', re.I)


def salvage(raw):
    """The answer of a reply whose JSON does not parse (cut at the token limit inside a long quote, 3 Nov 2026:
    most of the first run's "no reply"): the "answer" field alone, read with a pattern."""
    m = ANSWER.search(raw or "")
    if not m:
        return None
    try:
        return {"answer": json.loads(m.group(1))}
    except ValueError:
        return None


def read_reply(reply, attr):
    """(answer, page, quote) from the model's JSON: True/False, or a frozenset of known options."""
    if not isinstance(reply, dict) or "answer" not in reply:
        return None, None, ""
    a = reply.get("answer")
    if attr["type"] == "boolean":
        a = str(a).strip().lower() in ("yes", "true") if a is not None else None
    else:
        known = {o.lower(): o for o in attr["options"]}
        items = a if isinstance(a, list) else ([a] if a else [])
        a = frozenset(known[str(x).strip().lower()] for x in items if str(x).strip().lower() in known)
    return a, reply.get("page"), str(reply.get("quote") or "")[:300]


def score(stats, attr, truth, answer):
    s = stats
    s["answers"] += 1
    if answer is None:
        s["no reply"] += 1
        return False
    if attr["type"] == "boolean":
        ok = answer == truth
        s["truth yes"] += truth; s["said yes"] += answer; s["yes and right"] += truth and answer
    else:
        ok = answer == truth
        for o in attr["options"]:
            s[f"option {o}: right"] += (o in answer) == (o in truth)
        s["truth empty"] += not truth; s["said empty"] += not answer
    s["right"] += ok
    return ok


def report(codes, cfg, stats, wrong, usage, cost):
    md = ["### Narrative evaluation: the model answers judgement attributes from the company's pages", "",
          "Validated at attribute level in ACT Core Climate. \"No Evidence\" counts as no (yes/no) or none (lists); "
          "non-English and insufficient evidence are left out.", "",
          "| Attribute | Answers | Right | No pages found | Analyst yes | Model yes | Yes found (recall) | Yes right (precision) |",
          "| --- | --: | --: | --: | --: | --: | --: | --: |"]
    for c in codes:
        a, s = cfg[c], stats[c]
        n = s["answers"] or 1
        if a["type"] == "boolean":
            md.append(f"| {c} {a['name']} | {s['answers']} | {s['right']} ({s['right'] / n:.0%}) | {s['no pages']} | {s['truth yes']} | "
                      f"{s['said yes']} | {s['yes and right'] / (s['truth yes'] or 1):.0%} | {s['yes and right'] / (s['said yes'] or 1):.0%} |")
        else:
            per = ", ".join(f"{o} {s[f'option {o}: right'] / n:.0%}" for o in a["options"])
            md.append(f"| {c} {a['name']} | {s['answers']} | {s['right']} ({s['right'] / n:.0%}) exact; per option: {per} | "
                      f"{s['no pages']} | {s['answers'] - s['truth empty']} with an option | {s['answers'] - s['said empty']} | | |")
    md += ["", f"Model calls: {usage['calls']} ({usage['model_errors']} failed); unreadable replies after a retry: "
           f"{sum(stats[c]['no reply'] for c in codes)}; cost about ${cost:.2f}.", ""]
    for c in codes:
        if wrong[c]:
            md += [f"Wrong, {c} (first {len(wrong[c])}):", ""] + [f"- {w}" for w in wrong[c]] + [""]
    return "\n".join(md) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=15, help="answers per attribute (0 = all)")
    ap.add_argument("--attributes", default="")
    ap.add_argument("--max-usd", type=float, default=2.0)
    a = ap.parse_args()
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    cfg = {c: v for c, v in json.load(open(CONFIG, encoding="utf-8"))["attributes"].items() if v.get("question")}
    codes = [c for c in cfg if not a.attributes or c in a.attributes.split(",")]
    token = os.environ.get("GOLD_TOKEN", "").strip()
    if not token or not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        open("narrative_eval.md", "w").write("### Narrative evaluation: GOLD_TOKEN or ANTHROPIC_API_KEY is not set; nothing run.\n")
        return 0
    from model_reader import call, parse_reply, cost
    from extract_eval import fy_end_month
    import psycopg
    run = qa.Run("narrative_eval.py")
    answers = gold_answers(token, codes)
    by = collections.defaultdict(list)
    for g in sorted(answers, key=lambda g: g["id"]):
        g["truth"] = truth_of(g["value"], g["null"], cfg[g["attribute"]]["type"])
        if g["truth"] is not None:
            by[g["attribute"]].append(g)
    # yes and no answers alternate, so a small sample still holds both
    for c in codes:
        pos = [g for g in by[c] if g["truth"]]; neg = [g for g in by[c] if not g["truth"]]
        mixed = [g for pair in zip(pos, neg) for g in pair] + pos[len(neg):] + neg[len(pos):]
        by[c] = mixed[:a.limit] if a.limit else mixed
    print(">>> " + "; ".join(f"{c}: {len(by[c])} answers" for c in codes), flush=True)
    stats = collections.defaultdict(collections.Counter); wrong = collections.defaultdict(list)
    usage = collections.Counter(); stop = ""
    with psycopg.connect(os.environ["SUPABASE_DB_URL"].strip(), connect_timeout=30) as conn:
        cur = conn.cursor(); cur.execute("set statement_timeout = '60s'")
        queue = [g for i in range(max((len(v) for v in by.values()), default=0)) for c in codes if i < len(by[c]) for g in [by[c][i]]]
        for g in queue:
            if cost(usage) >= a.max_usd:
                stop = "the cost limit"; break
            attr, s = cfg[g["attribute"]], stats[g["attribute"]]
            try:
                pages = company_pages(cur, g["wba"], g["year"], attr["queries"], fy_end_month(cur, g["wba"]))
            except Exception:
                conn.rollback(); cur.execute("set statement_timeout = '60s'"); s["database did not answer"] += 1; continue
            if not pages:
                s["no pages"] += 1; answer = (False if attr["type"] == "boolean" else frozenset()); page, quote = None, ""
            else:
                try:
                    for attempt in range(2):          # one retry: an empty or unreadable reply is usually an overload
                        raw = call([{"role": "user", "content": prompt(attr, pages, g["wba"])}], usage, system=SYSTEM, max_tokens=800)
                        reply = parse_reply(raw)
                        if not (isinstance(reply, dict) and "answer" in reply):
                            reply = salvage(raw)
                        if reply:
                            break
                        s["unreadable replies"] += 1
                except Exception as e:
                    if type(e).__name__ == "ModelRefused":
                        stop = f"the API refused a call ({str(e)[:120]})"; break
                    raise
                if not reply and len(wrong[g["attribute"]]) < 3:
                    print(f"    unreadable reply for {g['id']}: {re.sub(r'\s+', ' ', raw or '(empty)')[:200]}", flush=True)
                answer, page, quote = read_reply(reply, attr)
            ok = score(s, attr, g["truth"], answer)
            if not ok and len(wrong[g["attribute"]]) < 12:
                t = (lambda v: ("yes" if v else "no") if isinstance(v, bool) else (", ".join(sorted(v)) or "none"))
                wrong[g["attribute"]].append(f"{g['id']}: analyst {t(g['truth'])}, model {t(answer) if answer is not None else 'no reply'}"
                                             + (f" (p. {page}: \"{quote[:120]}\")" if quote else "")
                                             + (f"; analyst's evidence: \"{g['evidence'][:120]}\"" if g["evidence"] else ""))
    md = report(codes, cfg, stats, wrong, usage, cost(usage))
    if stop:
        md += f"\nStopped at {stop}.\n"
    open("narrative_eval.md", "w").write(md)
    print(md, flush=True)
    for c in codes:
        s = stats[c]
        if s["answers"]:
            run.evaluation(f"narrative {c} {cfg[c]['name']}", s["answers"], s["right"] / s["answers"], version="model reader",
                           high_stakes=(s["yes and right"] / s["truth yes"]) if cfg[c]["type"] == "boolean" and s["truth yes"] else None,
                           suite="ACT Core validated answers",
                           notes="accuracy: the model's answer equals the analyst's (No Evidence = no). high_stakes_accuracy: "
                                 "share of the analyst's yes the model found (yes/no attributes).")
    run.finish("partial" if stop else "ok", rows_in=len(queue), rows_out=sum(s["right"] for s in stats.values()),
               counters={"cost in USD (estimate)": round(cost(usage), 3), **{c: dict(stats[c]) for c in codes}})
    return 1 if stop.startswith("the API") else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("narrative_eval.md", "w").write("### Narrative evaluation: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
