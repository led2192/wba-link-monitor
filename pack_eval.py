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

--read (narrative only, ANTHROPIC_API_KEY, billed; 6 Nov 2026): the model then answers each question three ways
on the same answers, every answer the analysts settled (yes and no alternating, as narrative_eval): today's
pages with one quote (narrative_eval as it is), the pack with one quote, and the pack with one to three quotes
of up to 60 words from different passages (the owner's review: quotes too short, evidence spread over several
documents). Scored as narrative_eval (the analyst's answer), with the share of right yes answers whose quotes
hold the analyst's evidence. --variants, --max-usd.
"""
import argparse, math, collections, json, os, re, sys, time, traceback

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


# ----- submodular choice of the pack (7 Nov 2026) -----
# The pack takes the best-scored passages, at most PER_PAGE from a page; near-duplicates (the same sentence in the
# letter, the summary and the chapter) can fill it and push out the methodological note. The submodular choice
# maximises relevance + SUB_LAMBDA * sum over roles of sqrt(passages of that role) - SUB_BETA * overlap with the
# passages already taken, greedily: the square root makes a second table passage worth less than a first note,
# and greedy selection of a monotone submodular function is within (1 - 1/e) of the best.
SUB_LAMBDA, SUB_BETA = 0.3, 0.5
NOTE = re.compile(r"\b(?:note|methodolog\w*|boundar\w*|scope of|calculat\w*|defin\w*|in accordance|according to|basis of|approach|consolidat\w*|policy|polic\w*|govern\w*|oversight|responsib\w*)\b", re.I)
FIGURES = re.compile(r"\d[\d,.]{2,}")


def role_of(text):
    """table (four figures or more), note (method, definition, policy or governance wording) or context."""
    if len(FIGURES.findall(text)) >= 4:
        return "table"
    return "note" if NOTE.search(text) else "context"


def pick_submodular(pas, scores, k=PACK, per_page=PER_PAGE, lam=SUB_LAMBDA, beta=SUB_BETA):
    """Greedy maximisation of relevance + role coverage - redundancy (see above). Same page cap as pick."""
    top = max(scores, default=0) or 1.0
    rel = [s / top for s in scores]
    words = [set(R.tokens(p[3])) for p in pas]
    roles = [role_of(p[3]) for p in pas]
    jac = lambda i, j: len(words[i] & words[j]) / (len(words[i] | words[j]) or 1)
    out, per, cov = [], collections.Counter(), collections.Counter()
    pool = [i for i in range(len(pas)) if scores[i] > 0]
    while len(out) < k:
        best, bi = -math.inf, None
        for i in pool:
            if i in out or per[(pas[i][0], pas[i][1])] >= per_page:
                continue
            gain = rel[i] + lam * (math.sqrt(cov[roles[i]] + 1) - math.sqrt(cov[roles[i]])) - beta * sum(jac(i, j) for j in out)
            if gain > best:
                best, bi = gain, i
        if bi is None:
            break
        out.append(bi); per[(pas[bi][0], pas[bi][1])] += 1; cov[roles[bi]] += 1
    return out


def score_passages(pas, words, extra, idfs, avg):
    w = {t: 1.0 for t in words}
    w.update({t: R.EXPAND_WEIGHT for t in extra if t not in w})
    out = []
    for *_, t in pas:
        tf = collections.Counter(R.tokens(t))
        out.append(R.bm25_weighted(tf, len(tf), avg, w, idfs))
    return out


# ----- the learned words of the pack (--save) -----

def save_terms(cases, idfs, avg):
    """What evidence_pack.py needs to build a narrative pack: per attribute the expansion words learned from the
    analysts' evidence of the other company folds (one part per fold, for scoring validated answers) and of all
    folds (for answers nobody has validated), and the idf of every query and expansion word."""
    out, words_all = {}, set()
    for code in sorted({c["g"]["attribute"] for c in cases if c["g"]["kind"] == "narrative"}):
        mine = [c for c in cases if c["g"]["attribute"] == code]
        words = set(R.query_terms(" ".join(mine[0]["g"]["queries"])))
        words_all |= words
        parts = {}
        for f in list(range(R.FOLDS)) + [None]:
            others = [{"attribute": code, "marks": x["marks"]} for x in mine if x["fold"] != f and x["target"]]
            terms = list(R.expansion(others, code, words)) if others else []
            words_all |= set(terms)
            parts["all" if f is None else str(f)] = {"terms": terms, "answers": len(others)}
        out[code] = parts
    return {"made_by": "pack_eval.py --save", "made": time.strftime("%Y-%m-%d"), "pack": PACK, "per_page": PER_PAGE,
            "avg": round(avg, 3), "idf": {t: round(idfs.get(t, 0.0), 6) for t in sorted(words_all)}, "attributes": out}


# ----- the model reading the pack (--read) -----

VARIANTS = ("today", "pack1", "pack3")      # today's pages and one quote; the pack and one quote; the pack and up to 3 longer quotes
PACK_SYSTEM = ("You read passages of a company's own reports and answer one question about what the company discloses. "
               "Answer only from the passages given. Reply with one JSON object only: no code fence, no text around it.")
QUOTE_WORDS = {1: 25, 3: 60}


def pack_prompt(attr, pas, chosen, docs, quotes):
    """The question, the pack's passages numbered (document type, year and page each), and the reply shape:
    one quote of 25 words as today, or one to three quotes of up to 60 words from different passages."""
    lines = [f"Question: {attr['question']}"]
    if attr["type"] == "boolean":
        lines.append('Answer "yes" only when a passage states it; "no" when the passages do not.')
        ans = '"yes" or "no"'
    else:
        lines.append("Options: " + "; ".join(attr["options"]) + ". Pick every option the passages show, none if they show none.")
        ans = "[the options shown]"
    if quotes == 1:
        shape = '{"answer": ' + ans + ', "passage": the passage number or null, "quote": "at most 25 words copied from the passage", "why": "ten words"}'
    else:
        lines.append(f"Back the answer with one to {quotes} quotes copied exactly, each up to {QUOTE_WORDS[quotes]} words, from different "
                     "passages (and different documents when the evidence is spread over them); together they should show the answer.")
        shape = ('{"answer": ' + ans + ', "quotes": [{"passage": the passage number, "quote": "up to '
                 + str(QUOTE_WORDS[quotes]) + ' words copied from it"}], "why": "ten words"}')
    lines += ["Passages:", ""]
    for n, j in enumerate(chosen, 1):
        lid, page, _, text = pas[j]
        dtype, year = docs.get(lid, ("", 0))
        lines += [f"<passage number=\"{n}\" document=\"{dtype or 'report'}{', ' + str(year) if year else ''}\" page=\"{page}\">", text, "</passage>", ""]
    lines.append("Reply: " + shape)
    return "\n".join(lines)


def reply_quotes(reply, chosen, pas):
    """[(library_id, page, quote)] from a reply: its "quotes" list, or its single "passage"/"page" and "quote"."""
    if not isinstance(reply, dict):
        return []
    items = reply.get("quotes") if isinstance(reply.get("quotes"), list) else [{"passage": reply.get("passage"), "quote": reply.get("quote")}]
    out = []
    for q in items:
        if not isinstance(q, dict) or not str(q.get("quote") or "").strip():
            continue
        try:
            j = chosen[int(q.get("passage")) - 1]
            lid, page = pas[j][0], pas[j][1]
        except (TypeError, ValueError, IndexError):
            lid, page = None, None
        out.append((lid, page, str(q["quote"])[:600]))
    return out


def read_one(c, variant, attr, usage, call, parse_reply):
    """(answer, quotes) of one variant for one answer."""
    if variant == "today":
        if not c["today4"]:
            return (False if attr["type"] == "boolean" else frozenset()), []
        content, system = NE.prompt(attr, c["today4"], c["g"]["wba"]), NE.SYSTEM
    else:
        if not c["chosen"]:
            return (False if attr["type"] == "boolean" else frozenset()), []
        content, system = pack_prompt(attr, c["pas"], c["chosen"], c["docs"], int(variant[-1])), PACK_SYSTEM
    reply = None
    for _ in range(2):
        raw = call([{"role": "user", "content": content}], usage, system=system, max_tokens=900)
        reply = parse_reply(raw)
        if not (isinstance(reply, dict) and "answer" in reply):
            reply = NE.salvage(raw)
        if reply:
            break
    answer, page, quote = NE.read_reply(reply, attr)
    if variant == "today":
        lid = next((p[0] for p in c["today4"] if str(p[2]) == str(page)), None)
        return answer, ([(lid, page, quote)] if quote else [])
    return answer, reply_quotes(reply, c["chosen"], c["pas"])


def read_all(cases, a):
    """Every variant on the same answers; the report section."""
    from model_reader import call, parse_reply, cost
    cfg = {k: v for k, v in json.load(open(NE.CONFIG, encoding="utf-8"))["attributes"].items() if v.get("question")}
    variants = [v for v in a.variants.split(",") if v in VARIANTS]
    st = {v: collections.defaultdict(collections.Counter) for v in variants}
    usage, stop = collections.Counter(), ""
    for i, c in enumerate(cases, 1):
        if cost(usage) >= a.max_usd:
            stop = f"the ${a.max_usd:g} cost limit after {i - 1:,} answers"; break
        if i % 50 == 0:
            print(f"  read {i:,}/{len(cases):,}, ${cost(usage):.2f}", flush=True)
        g = c["g"]; attr = cfg[g["attribute"]]
        evg = grams(g.get("evidence") or "")
        got = {}
        for v in variants:
            answer, quotes = read_one(c, v, attr, usage, call, parse_reply)
            s = st[v][g["attribute"]]
            ok = NE.score(s, attr, g["truth"], answer)
            got[v] = ok
            if ok and g["truth"] and quotes:
                s["right with a quote"] += 1
                s["quote words"] += sum(len(q.split()) for *_, q in quotes)
                s["quotes"] += len(quotes)
                s["quotes from two or more documents"] += len({l for l, _, _ in quotes if l}) >= 2
                if evg:
                    s["right yes with analyst evidence"] += 1
                    s["quote holds analyst evidence"] += any(holds(q, evg) for *_, q in quotes)
        for v in variants[1:]:
            st[v][g["attribute"]]["right where today wrong"] += got[v] and not got.get("today", True)
            st[v][g["attribute"]]["wrong where today right"] += got.get("today", False) and not got[v]
    pct = lambda x, n: f"{x / n:.0%}" if n else "-"
    codes = sorted({c["g"]["attribute"] for c in cases})
    md = ["", "### The model reading today's pages and the pack, on the same answers", "",
          f"{os.environ.get('READER_MODEL', 'claude-haiku-5-5')}; validated answers of ACT Core Climate, yes and no alternating. "
          "today: today's pages and one quote of 25 words; pack1: the pack and one quote; pack3: the pack and one to three quotes of "
          "up to 60 words from different passages. Right: the model's answer equals the analyst's. Quote holds the evidence: among "
          "right yes answers with analyst evidence, a quote shares 6 consecutive words with it.", "",
          "| Attribute | Answers | " + " | ".join(f"Right {v}" for v in variants) + " | " + " | ".join(f"Yes found {v}" for v in variants)
          + " | " + " | ".join(f"Quote holds the evidence {v}" for v in variants) + " | " + " | ".join(f"Words quoted {v}" for v in variants) + " |",
          "| --- | --: |" + " --: |" * (4 * len(variants))]
    tot = {v: collections.Counter() for v in variants}
    for code in codes + ["All"]:
        ss = {v: (tot[v] if code == "All" else st[v][code]) for v in variants}
        if code != "All":
            for v in variants:
                tot[v].update(st[v][code])
        n = ss[variants[0]]["answers"]
        md.append(f"| {code} | {n:,} | " + " | ".join(pct(ss[v]["right"], ss[v]["answers"]) for v in variants) + " | "
                  + " | ".join(pct(ss[v]["yes and right"], ss[v]["truth yes"]) for v in variants) + " | "
                  + " | ".join(pct(ss[v]["quote holds analyst evidence"], ss[v]["right yes with analyst evidence"]) for v in variants) + " | "
                  + " | ".join((f"{ss[v]['quote words'] / ss[v]['right with a quote']:.0f}" if ss[v]["right with a quote"] else "-") for v in variants) + " |")
    if "today" in variants:
        md += ["", "Against today (the rule: no attribute more than 3 points worse):", ""]
        for v in variants[1:]:
            diffs = {code: 100 * (st[v][code]["right"] / max(st[v][code]["answers"], 1) - st["today"][code]["right"] / max(st["today"][code]["answers"], 1))
                     for code in codes}
            worst = min(diffs.values()) if diffs else 0
            all_d = 100 * (tot[v]["right"] / max(tot[v]["answers"], 1) - tot["today"]["right"] / max(tot["today"]["answers"], 1))
            md.append(f"- {v}: {all_d:+.1f} points overall, worst attribute {worst:+.1f}, right where today was wrong "
                      f"{tot[v]['right where today wrong']:,}, wrong where today was right {tot[v]['wrong where today right']:,}, "
                      f"quotes from two or more documents {tot[v]['quotes from two or more documents']:,}: "
                      + ("qualifies" if worst >= -3 else "does not qualify"))
    md += ["", f"Model calls: {usage.get('calls', 0) or '?'}; cost about ${cost(usage):.2f}." + (f" Stopped at {stop}." if stop else "")]
    return md


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=120, help="answers per attribute (0 = all)")
    ap.add_argument("--kinds", default="narrative,number")
    ap.add_argument("--pack", type=int, default=PACK)
    ap.add_argument("--per-page", type=int, default=PER_PAGE)
    ap.add_argument("--budget", type=float, default=80)
    ap.add_argument("--read", action="store_true", help="narrative only: also have the model read today's pages and the pack, on the same answers")
    ap.add_argument("--variants", default=",".join(VARIANTS), help="with --read: " + ", ".join(VARIANTS))
    ap.add_argument("--max-usd", type=float, default=3.0, help="with --read: cost limit of the model calls")
    ap.add_argument("--save", default="", help="narrative: write the pack's learned words for evidence_pack.py (narrative_eval --evidence pack3)")
    a = ap.parse_args()
    if a.read:
        a.kinds = "narrative"
    run = qa.Run("pack_eval.py")
    token = os.environ["GOLD_TOKEN"].strip()
    kinds = a.kinds.split(",")
    todo = []
    if "narrative" in kinds:
        cfg = {c: v for c, v in json.load(open(NE.CONFIG, encoding="utf-8"))["attributes"].items() if v.get("question")}
        gold = NE.gold_answers(token, list(cfg))
        if a.read:      # every answer the analysts settled, yes and no alternating as in narrative_eval
            for g in gold:
                g["truth"] = NE.truth_of(g["value"], g["null"], cfg[g["attribute"]]["type"])
            gold = [g for g in sorted(gold, key=lambda g: g["id"]) if g["truth"] is not None]
        else:
            gold = [g for g in gold if (g.get("evidence") or "").strip()]
        by = collections.defaultdict(list)
        for g in gold:
            by[g["attribute"]].append(g)
        if a.read:
            for c in list(by):
                pos = [g for g in by[c] if g["truth"]]; neg = [g for g in by[c] if not g["truth"]]
                by[c] = [g for pair in zip(pos, neg) for g in pair] + pos[len(neg):] + neg[len(pos):]
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
                today4 = None
                if g["kind"] == "narrative":
                    today4 = NE.company_pages(cur, g["wba"], g["year"], g["queries"], fy, "ts")
                    today = [(lid, page, body[:NE.PAGE_CHARS]) for lid, _, page, body in today4]
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
                          "pas": pas, "target": {j for j, x in enumerate(pas) if test(x[3])},
                          "today4": today4, "docs": {d[0]: (d[1], d[2]) for d in docs}})
    print(f">>> {len(cases):,} answers with documents, {(time.time() - t0) / 60:.0f} min", flush=True)

    texts = [x[3] for c in cases for x in c["pas"]]
    df = collections.Counter(t for x in texts for t in set(R.tokens(x)))
    idfs = {t: R.idf(n, len(texts)) for t, n in df.items()}
    avg = sum(len(set(R.tokens(x))) for x in texts) / (len(texts) or 1)
    for c in cases:
        c["marks"] = R.marking_terms({j: x[3] for j, x in enumerate(c["pas"])}, c["target"]) if c["target"] else {}
    if a.save:
        model = save_terms(cases, idfs, avg)
        with open(a.save, "w", encoding="utf-8") as f:
            json.dump(model, f, indent=1, sort_keys=True)
        # Run artifacts cannot be fetched from every place the file is needed (6 Nov 2026): also printed between
        # markers. Words and weights only, no company and no answer.
        print(">>> PACK_TERMS_JSON_BEGIN\n" + json.dumps(model, sort_keys=True) + "\n>>> PACK_TERMS_JSON_END", flush=True)
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
        c["chosen"] = chosen
        if g["kind"] == "narrative" and not (g.get("evidence") or "").strip():
            continue                     # (--read) an answer with no evidence text cannot be found or missed
        s = stats[g["attribute"]]
        s["kind " + g["kind"]] = 1
        s["answers"] += 1
        s["candidate passages hold it"] += bool(c["target"])
        s["today"] += c["today_hit"]
        s["pack"] += any(j in c["target"] for j in chosen)
        if g["kind"] == "narrative":
            sub = pick_submodular(c["pas"], sc, a.pack, a.per_page)
            s["submodular"] += any(j in c["target"] for j in sub)
            s["submodular docs"] += len({c["pas"][j][0] for j in sub})
            s["submodular roles"] += len({role_of(c["pas"][j][3]) for j in sub})
            s["pack roles"] += len({role_of(c["pas"][j][3]) for j in chosen})
        s["either"] += c["today_hit"] or any(j in c["target"] for j in chosen)
        s["today words"] += c["today_words"]; s["pack words"] += sum(len(c["pas"][j][3].split()) for j in chosen)
        s["today pages"] += c["today_pages"]; s["pack pages"] += len({c["pas"][j][:2] for j in chosen})
        s["today docs"] += c["today_docs"]; s["pack docs"] += len({c["pas"][j][0] for j in chosen})
    md = report(stats, a)
    sub = [(c, st) for c, st in stats.items() if st.get("submodular") is not None and st["answers"] and "kind narrative" in st]
    if sub:
        md += ["", "#### The pack chosen submodularly (relevance + coverage of table, note and context passages - overlap), same answers", "",
               "| Attribute | Answers | Evidence in the pack | Evidence in the submodular pack | Documents, pack / submodular | Roles, pack / submodular |",
               "| --- | --: | --: | --: | --: | --: |"]
        for c, st in sub:
            n = st["answers"]
            md.append(f"| {c} | {n:,} | {st['pack'] / n:.0%} | {st['submodular'] / n:.0%} | {st['pack docs'] / n:.1f} / {st['submodular docs'] / n:.1f} | "
                      f"{st['pack roles'] / n:.1f} / {st['submodular roles'] / n:.1f} |")
    if a.read:
        md += read_all(cases, a)
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
