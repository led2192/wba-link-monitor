#!/usr/bin/env python3
"""
vocab_mining.py: the words that set apart the page where an attribute's value is printed, learned from the
validated answers. Writes a weighted vocabulary per attribute (vocabulary.json) for the reader's retrieval.

The reader finds pages through the labels in attributes.json, as exact phrases. Most pages it misses print
the value with no label on them (retrieval diagnosis of 29 Oct 2026: 42 of 46 missed pages), because
reports name the same figure in many ways. Instead of more hand-written phrases, this script learns which
words are typical of the right page:

  positives  for every validated answer, the pages of the company's documents for that year (or the next)
             that print the value, in any scale;
  negatives  up to NEG_PER_ANSWER other pages of the same documents, drawn at random;
  terms      every word and pair of adjacent words on a page, counted once per page;
  weight     the log-odds of a term on a positive page against a negative one (smoothed), kept when the
             term is on at least MIN_SHARE of the positive pages, in at least MIN_COMPANIES companies.

The first EVAL_HOLDOUT answers of each attribute, in the order the Generic reader takes them, are left out,
so the reader's scored sample is never used to learn the words it is scored with.

Read-only: the gold bases (GOLD_TOKEN), the evidence database (SUPABASE_DB_URL). Writes vocab_mining.md
(also a run artifact; the vocabulary is printed in it as JSON) and the run's counters in Pipeline QA.
Options: --limit N answers per attribute after the holdout (default 300), --attributes codes,
--budget minutes (100), --commit (Pipeline QA).
"""
import argparse, collections, json, math, os, sys, time, traceback

import qa
from label_mining import number_spans, words
from value_eval import CURRENCY_SCALES, EMISSION_SCALES, printed_forms, tsquery_for

HERE = os.path.dirname(os.path.abspath(__file__))
EVAL_HOLDOUT = 40                 # the Generic reader's pull request sample per attribute
NEG_PER_ANSWER = 15
MIN_SHARE = 0.08
MIN_COMPANIES = 4
TOP = 40
STOP = set("""a an and are as at be by for from has have in into is it its of on or our per than that the their
this to was were which with we us not all also other such these those any may can""".split())


def terms(text):
    """The words of a page and its pairs of adjacent words, as a set; stop words alone are left out."""
    ws = words(text)
    out = {w for w in ws if w not in STOP and len(w) > 1 or w in ("1", "2", "3")}
    out |= {f"{a} {b}" for a, b in zip(ws, ws[1:]) if not (a in STOP and b in STOP)}
    return out


def weigh(pos, neg, n_pos, n_neg, companies, top=TOP):
    """[(term, weight)] best first: log-odds of a term on a positive page against a negative page, for terms
    on at least MIN_SHARE of the positive pages and in at least MIN_COMPANIES companies."""
    out = []
    for t, k in pos.items():
        if k < max(3, MIN_SHARE * n_pos) or len(companies[t]) < MIN_COMPANIES:
            continue
        w = math.log((k + 0.5) / (n_pos + 1)) - math.log((neg.get(t, 0) + 0.5) / (n_neg + 1))
        if w > 0.7:
            out.append((t, round(w, 2)))
    out.sort(key=lambda x: -x[1])
    kept = []
    for t, w in out:                     # a single word already covered by a stronger pair adds little
        if " " not in t and any(t in k.split() and wk >= w for k, wk in kept):
            continue
        kept.append((t, w))
        if len(kept) >= top:
            break
    return kept


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--attributes", default="")
    ap.add_argument("--budget", type=int, default=100)
    a = ap.parse_args()
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    token = os.environ.get("GOLD_TOKEN", "").strip()
    if not token:
        open("vocab_mining.md", "w").write("### Vocabulary mining: GOLD_TOKEN is not set; nothing run.\n")
        return 0
    import psycopg
    from value_eval import gold_values
    spec = json.load(open(os.path.join(HERE, "attributes.json"), encoding="utf-8"))
    gold_cfg = json.load(open(os.path.join(HERE, "gold_attributes.json"), encoding="utf-8"))
    codes = [c for c, v in spec["attributes"].items() if v["type"] == "number" and (not a.attributes or c in a.attributes.split(","))]
    run = qa.Run("vocab_mining.py")
    answers = [g for base in gold_cfg["bases"].values() for g in gold_values(token, base, codes)
               if g["value"] and not g["unit"].lower().startswith(("%", "percent"))]
    by = collections.defaultdict(list)
    for g in answers:
        by[g["attribute"]].append(g)
    learn = {c: by[c][EVAL_HOLDOUT:EVAL_HOLDOUT + a.limit] for c in codes}
    todo = [q[i] for i in range(max((len(q) for q in learn.values()), default=0)) for q in learn.values() if i < len(q)]
    print(f">>> {len(todo):,} answers to learn from ({', '.join(f'{c} {len(learn[c])}' for c in codes)}); "
          f"the first {EVAL_HOLDOUT} of each attribute are held out", flush=True)

    pos = {c: collections.Counter() for c in codes}; neg = {c: collections.Counter() for c in codes}
    comp = {c: collections.defaultdict(set) for c in codes}
    n = {c: collections.Counter() for c in codes}
    t0, cut = time.time(), False
    with psycopg.connect(os.environ["SUPABASE_DB_URL"].strip(), connect_timeout=30) as conn:
        cur = conn.cursor()
        cur.execute("set statement_timeout = '60s'")
        for i, g in enumerate(todo, 1):
            if time.time() - t0 > a.budget * 60:
                cut = True; break
            if i % 100 == 0:
                print(f"  {i:,}/{len(todo):,} in {(time.time() - t0) / 60:.0f} min", flush=True)
            c = g["attribute"]; attr = spec["attributes"][c]
            scales = EMISSION_SCALES if attr.get("unit_family") == "tco2e" else CURRENCY_SCALES
            forms = printed_forms(g["value"], scales)
            if not forms:
                continue
            try:
                cur.execute("select library_id from public.documents where wba_id = %s and year in (%s, %s)", (g["wba"], g["year"], g["year"] + 1))
                ids = [r[0] for r in cur.fetchall()]
                if not ids:
                    continue
                q = " | ".join(f"({tsquery_for(v, d)})" for v, _, d in forms)
                cur.execute("select library_id, page, body from public.pages where library_id = any(%s) and words @@ to_tsquery('simple', %s) limit 30", (ids, q))
                hits = [(lid, p, b) for lid, p, b in cur.fetchall() if any(number_spans(b, v, d) for v, _, d in forms)]
                if not hits:
                    continue
                keys = {(lid, p) for lid, p, _ in hits}
                cur.execute("select library_id, page, body from public.pages where library_id = any(%s) order by random() limit %s",
                            (list({lid for lid, _, _ in hits}), NEG_PER_ANSWER + len(hits)))
                others = [b for lid, p, b in cur.fetchall() if (lid, p) not in keys][:NEG_PER_ANSWER]
            except Exception:
                conn.rollback(); cur.execute("set statement_timeout = '60s'")
                n[c]["database did not answer"] += 1; continue
            n[c]["answers with a page"] += 1
            page_terms = set()
            for _, _, b in hits:
                page_terms |= terms(b)
            pos[c].update(page_terms); n[c]["positive"] += 1      # one positive per answer: its pages together
            for t in page_terms:
                comp[c][t].add(g["wba"])
            for b in others:
                neg[c].update(terms(b)); n[c]["negative"] += 1

    vocab = {c: weigh(pos[c], neg[c], n[c]["positive"], n[c]["negative"], comp[c]) for c in codes}
    md = ["### Vocabulary learned from the validated answers" + ("" if a.commit else " (dry run)"), "",
          f"Learned from up to {a.limit} answers per attribute after the first {EVAL_HOLDOUT}, which the Generic reader scores and are held out"
          + ("; stopped at the time budget" if cut else "") + ".", "",
          "| Attribute | Answers with a page | Positive | Negative pages | Top words (weight) |", "| --- | --: | --: | --: | --- |"]
    for c in codes:
        md.append(f"| {c} {spec['attributes'][c]['name']} | {n[c]['answers with a page']:,} | {n[c]['positive']:,} | {n[c]['negative']:,} | "
                  + ", ".join(f"{t} ({w})" for t, w in vocab[c][:12]) + " |")
    md += ["", "<details><summary>vocabulary.json</summary>", "", "```json",
           json.dumps({"learned": time.strftime("%Y-%m-%d"), "attributes": {c: vocab[c] for c in codes}}, ensure_ascii=False, indent=1),
           "```", "", "</details>", ""]
    open("vocab_mining.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    run.finish("ok", rows_in=len(todo), rows_out=sum(len(v) for v in vocab.values()), counters={c: dict(n[c]) for c in codes})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("vocab_mining.md", "w").write("### Vocabulary mining: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
