#!/usr/bin/env python3
"""
pack_eval.py: would a pack of short passages, chosen without a model, put the evidence in front of the reader
more often than the pages we send today?

The owner's proposal of 5 Nov 2026, after reviewing 30 narrative disagreements: the reader sees a few whole
pages of a few documents and answers from one short quote of one page, while the evidence for an attribute
often sits on several pages and documents. Instead, cut the candidate pages of all of the year's documents
into passages of about 120 words (evidence_chunks.passages), score the passages for the attribute without a
model (BM25 over the attribute's words plus the words that mark the analysts' evidence, learned from the
other companies by fold, as in rank_eval), and keep the best PACK passages, at most PER_PAGE from one page.
Fifteen passages are about 1,800 words: less text than today's pages, spread over up to fifteen pages.

Measured on the same validated answers, no model calls:
  narrative attributes (gold_attributes.json, with a question): is the analyst's evidence text (any run of 6
      consecutive words) in what the model is shown today (narrative_eval.company_pages, the first PAGE_CHARS
      of each page), and in the pack?
  number attributes (attributes.json): is the validated number on today's 8 model pages (select_eval base),
      and in the pack?
Plus the words shown, and how many pages and documents each covers. Counts and shares only in the report.

Read-only. Env: SUPABASE_DB_URL, GOLD_TOKEN; AIRTABLE_QA_TOKEN for the run record.
Options: --limit N answers per attribute, --kinds narrative,number, --pack, --per-page, --budget minutes.
"""
import argparse, collections, json, os, re, sys, time, traceback

import qa
import extract_eval as E
import narrative_eval as NE
import rank_eval as R
import select_eval as S
from evidence_chunks import passages
from value_eval import found_on, printed_forms, scales_for

PACK, PER_PAGE = 15, 2
PASSAGE_WORDS = 120
CANDIDATE_PAGES = 60             # pages cut into passages per answer (any query word, BM25), plus the strict ones
GRAM = 6


def grams(text, n=GRAM):
    w = re.findall(r"[^\W_]+", (text or "").lower())
    return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}


def holds(text, evg):
    return bool(evg) and bool(evg & grams(text))


def year_docs(cur, wba, year, fy):
    want = E.document_year(year, fy)
    cur.execute("select library_id, coalesce(doc_type, ''), coalesce(year, 0) from public.documents "
                "where wba_id = %s and year in (%s, %s)", (wba, want, want + 1))
    return cur.fetchall()


def candidate_pages(cur, ids, queries):
    """[(lid, page, body)]: the strict pages (every word of one wording) and the BM25 any-word pages."""
    from page_ranker import RANKER
    q = NE.or_query(queries)
    cur.execute("select library_id, page, body from public.pages, to_tsquery('simple', %s) q "
                "where library_id = any(%s) and words @@ q order by ts_rank_cd(words, q, 32) desc limit 20", (q, ids))
    out = {(l, p): b or "" for l, p, b in cur.fetchall()}
    for lid, page, body, _ in RANKER.rank(cur, ids, queries, CANDIDATE_PAGES):
        out.setdefault((lid, page), body or "")
    return [(l, p, b) for (l, p), b in out.items()]


def line_passages(body, target=PASSAGE_WORDS):
    """Passages of whole lines, about `target` words each, line breaks kept: a table row stays one line with its
    label and its figures (the sentence splitter of evidence_chunks joins a table into one run and breaks rows;
    first sample, 5 Nov 2026: Scope 1 found in 1 pack of 10 against 7 of today's pages)."""
    out, cur, n = [], [], 0
    for ln in (body or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        cur.append(ln); n += len(ln.split())
        if n >= target:
            out.append("\n".join(cur)); cur, n = [], 0
    if cur:
        if out and n < target // 3:
            out[-1] += "\n" + "\n".join(cur)
        else:
            out.append("\n".join(cur))
    return out


def cut(pages, kind="narrative"):
    """[(lid, page, n, text)] passages of the pages: by sentences for narrative attributes, by lines for numbers."""
    split = passages if kind == "narrative" else line_passages
    return [(lid, page, n, t) for lid, page, body in pages for n, t in enumerate(split(body))]


def pick(pas, scores, k=PACK, per_page=PER_PAGE):
    per, out = collections.Counter(), []
    for i in sorted(range(len(pas)), key=lambda i: -scores[i]):
        if scores[i] <= 0:
            break
        key = (pas[i][0], pas[i][1])
        if per[key] >= per_page:
            continue
        out.append(i); per[key] += 1
        if len(out) >= k:
            break
    return out


def score_passages(pas, words, extra, idfs, avg):
    w = {t: 1.0 for t in words}
    w.update({t: R.EXPAND_WEIGHT for t in extra if t not in w})
    out = []
    for *_, t in pas:
        tf = collections.Counter(R.tokens(t))
        out.append(R.bm25_weighted(tf, len(tf), avg, w, idfs))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=120, help="answers per attribute (0 = all)")
    ap.add_argument("--kinds", default="narrative,number")
    ap.add_argument("--pack", type=int, default=PACK)
    ap.add_argument("--per-page", type=int, default=PER_PAGE)
    ap.add_argument("--budget", type=float, default=80)
    a = ap.parse_args()
    run = qa.Run("pack_eval.py")
    token = os.environ["GOLD_TOKEN"].strip()
    kinds = a.kinds.split(",")
    todo = []
    if "narrative" in kinds:
        cfg = {c: v for c, v in json.load(open(NE.CONFIG, encoding="utf-8"))["attributes"].items() if v.get("question")}
        gold = [g for g in NE.gold_answers(token, list(cfg)) if (g.get("evidence") or "").strip()]
        by = collections.defaultdict(list)
        for g in gold:
            by[g["attribute"]].append(g)
        for c in cfg:
            todo += [dict(g, kind="narrative", queries=cfg[c]["queries"]) for g in (by[c][:a.limit] if a.limit else by[c])]
    spec = json.load(open(E.SPEC, encoding="utf-8"))
    if "number" in kinds:
        gold_cfg = json.load(open(os.path.join(os.path.dirname(E.SPEC), "gold_attributes.json"), encoding="utf-8"))
        codes = [c for c, v in spec["attributes"].items() if v["type"] == "number"]
        from value_eval import gold_values
        answers = [g for base in gold_cfg["bases"].values() for g in gold_values(token, base, codes)
                   if g["value"] and not g["unit"].lower().startswith(("%", "percent"))]
        by = collections.defaultdict(list)
        for g in answers:
            by[g["attribute"]].append(g)
        for c in codes:
            todo += [dict(g, kind="number", queries=spec["attributes"][c].get("queries") or spec["attributes"][c]["labels"]) for g in (by[c][:a.limit] if a.limit else by[c])]
    print(f">>> {len(todo):,} answers", flush=True)
    t0, cases, failed = time.time(), [], 0
    with __import__("evidence_db").connect() as conn:
        cur = conn.cursor(); cur.execute("set statement_timeout = '30s'")
        for i, g in enumerate(todo, 1):
            if time.time() - t0 > a.budget * 60:
                print(f">>> stopped at the {a.budget:g}-minute budget after {i - 1:,} answers", flush=True); break
            if i % 100 == 0:
                print(f"  {i:,}/{len(todo):,}, {(time.time() - t0) / 60:.0f} min", flush=True)
            try:
                fy = E.fy_end_month(cur, g["wba"])
                docs = year_docs(cur, g["wba"], g["year"], fy)
                if not docs:
                    continue
                ids = [d[0] for d in docs]
                if g["kind"] == "narrative":
                    today = [(lid, page, body[:NE.PAGE_CHARS]) for lid, _, page, body in NE.company_pages(cur, g["wba"], g["year"], g["queries"], fy, "ts")]
                    evg = grams(g["evidence"])
                    test = lambda text: holds(text, evg)
                else:
                    attr = spec["attributes"][g["attribute"]]
                    d2, weight = E.year_documents(cur, g["wba"], g["year"], attr)
                    pool = [(s, lid, page, body or "", top) for s, lid, page, body, top in
                            E.keep_pages(E.ranked_pages(cur, [d[0] for d in d2], weight, g["year"], attr, spec, {}, None,
                                                        siblings=E.sibling_pages(cur, g["wba"], g["year"], attr, spec)))]
                    chosen = set(S.choose(pool))
                    today = [(lid, page, body) for _, lid, page, body, _ in pool if (lid, page) in chosen]
                    forms = printed_forms(g["value"], scales_for(attr.get("unit_family")))
                    test = lambda text, forms=forms: any(found_on(text, v) for v, _, _ in forms)
                pas = cut(candidate_pages(cur, ids, g["queries"]), g["kind"])
                failed = 0
            except Exception as e:
                failed += 1
                conn.rollback(); cur.execute("set statement_timeout = '30s'")
                if failed >= E.DB_FAILS_IN_A_ROW:
                    raise RuntimeError(f"the evidence database did not answer {failed} times in a row ({type(e).__name__})")
                continue
            cases.append({"g": g, "fold": R.fold_of(g["wba"]), "today_hit": any(test(b) for _, _, b in today),
                          "today_words": sum(len(b.split()) for _, _, b in today),
                          "today_pages": len({(l, p) for l, p, _ in today}), "today_docs": len({l for l, _, _ in today}),
                          "pas": pas, "target": {j for j, x in enumerate(pas) if test(x[3])}})
    print(f">>> {len(cases):,} answers with documents, {(time.time() - t0) / 60:.0f} min", flush=True)

    texts = [x[3] for c in cases for x in c["pas"]]
    df = collections.Counter(t for x in texts for t in set(R.tokens(x)))
    idfs = {t: R.idf(n, len(texts)) for t, n in df.items()}
    avg = sum(len(set(R.tokens(x))) for x in texts) / (len(texts) or 1)
    for c in cases:
        c["marks"] = R.marking_terms({j: x[3] for j, x in enumerate(c["pas"])}, c["target"]) if c["target"] else {}
    stats = collections.defaultdict(collections.Counter)
    exp_cache = {}
    for c in cases:
        g = c["g"]
        key = (g["attribute"], c["fold"])
        words = R.query_terms(" ".join(g["queries"]))
        if key not in exp_cache:
            others = [{"attribute": g["attribute"], "marks": x["marks"]} for x in cases
                      if x["g"]["attribute"] == g["attribute"] and x["fold"] != c["fold"] and x["target"]]
            exp_cache[key] = R.expansion(others, g["attribute"], set(words))
        sc = score_passages(c["pas"], words, exp_cache[key], idfs, avg)
        chosen = pick(c["pas"], sc, a.pack, a.per_page)
        s = stats[g["attribute"]]
        s["kind " + g["kind"]] = 1
        s["answers"] += 1
        s["candidate passages hold it"] += bool(c["target"])
        s["today"] += c["today_hit"]
        s["pack"] += any(j in c["target"] for j in chosen)
        s["either"] += c["today_hit"] or any(j in c["target"] for j in chosen)
        s["today words"] += c["today_words"]; s["pack words"] += sum(len(c["pas"][j][3].split()) for j in chosen)
        s["today pages"] += c["today_pages"]; s["pack pages"] += len({c["pas"][j][:2] for j in chosen})
        s["today docs"] += c["today_docs"]; s["pack docs"] += len({c["pas"][j][0] for j in chosen})
    md = report(stats, a)
    open("pack_eval.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    run.finish("ok", rows_in=len(todo), rows_out=len(cases), counters={k: dict(v) for k, v in stats.items()})
    return 0


def report(stats, a):
    pct = lambda x, n: f"{x / n:.0%}" if n else ""
    avg = lambda x, n: f"{x / n:,.0f}" if n else ""
    md = ["### Evidence packs: short passages chosen without a model, against today's pages", "",
          f"Pack: the best {a.pack} passages of about 120 words, at most {a.per_page} from one page, from the candidate pages of all "
          "the year's documents; scored by BM25 over the attribute's words plus words learned from the other companies' "
          "evidence. Narrative: the analyst's evidence text (6 consecutive words) in what the model sees. Number: the "
          "validated number. Same answers for both.", "",
          "| Attribute | Kind | Answers | Today | Pack | Either | Words today / pack | Pages today / pack | Documents today / pack |",
          "| --- | --- | --: | --: | --: | --: | --: | --: | --: |"]
    tot = collections.defaultdict(collections.Counter)
    for code, s in sorted(stats.items()):
        kind = "narrative" if s["kind narrative"] else "number"
        n = s["answers"]
        md.append(f"| {code} | {kind} | {n:,} | {pct(s['today'], n)} | {pct(s['pack'], n)} | {pct(s['either'], n)} | "
                  f"{avg(s['today words'], n)} / {avg(s['pack words'], n)} | {avg(s['today pages'], n)} / {avg(s['pack pages'], n)} | "
                  f"{avg(s['today docs'], n)} / {avg(s['pack docs'], n)} |")
        tot[kind].update({k: v for k, v in s.items() if not k.startswith("kind")})
    for kind, s in tot.items():
        n = s["answers"]
        md.append(f"| All {kind} | | {n:,} | {pct(s['today'], n)} | {pct(s['pack'], n)} | {pct(s['either'], n)} | "
                  f"{avg(s['today words'], n)} / {avg(s['pack words'], n)} | {avg(s['today pages'], n)} / {avg(s['pack pages'], n)} | "
                  f"{avg(s['today docs'], n)} / {avg(s['pack docs'], n)} |")
    md += ["", "\"Either\" is today's pages plus the pack: what sending both would reach. A pack that matches or beats today "
           "on every attribute with fewer words is the candidate for a model-reading run."]
    return md


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("pack_eval.md", "w").write("### Evidence packs: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
