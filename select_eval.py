#!/usr/bin/env python3
"""
select_eval.py: which way of choosing the model reader's pages puts the validated number in front of it?

The model reader (Propose values, extract_eval --reader model) is shown MODEL_PAGES (8) pages of a company's
year: half chosen by the rule candidates (pages_for_model), half by rank. Six ideas from the information
retrieval review (5 Nov 2026), measured side by side on the same validated answers, before any of them is
switched on (CLAUDE.md, no going back):

  base       today's choice (pages_for_model on today's ranking);
  expand     query expansion: the rank half reordered by fusing today's rank with BM25 over the words that mark
             the analysts' cited pages, learned from the other companies (rank_eval: 69% top 3 against 44%);
  doctype    a document-type prior learned from where the analysts' numbers are (which types hold the cited
             pages of this attribute, against how often each type is in the pool), from the other companies;
  learned    a logistic regression over page features (today's rank, rule candidate, type prior, expansion
             score, meaning, position, numbers), trained on the other companies' cited pages;
  embed      meaning: a multilingual sentence embedding of the attribute against each page, fused with today's
             rank (sentence-transformers, paraphrase-multilingual-MiniLM-L12-v2, on the runner);
  translate  pages found with the attribute's words in the document's own language (translated once per
             attribute and language by the model), fused into the pool for documents not in English;
  locate     a first model pass: the model sees short extracts of up to LOCATE_POOL candidate pages and picks
             the 8 most likely to print the figure (the "locate pass" of another team: 96% of answer pages sent).
  horizon    (6 Nov 2026, from the owner's research on search engines: Google's re-ranking "twiddlers") a page
             where the future outweighs the year asked (target words and years three or more years after the
             year, against mentions of the year and the year before) is moved HORIZON_PUSH places down, so a
             page of targets ("only a 2025-2030 target is printed") gives way to a page of actuals. No model,
             nothing learned.
"Learned from the other companies" is by company fold (rank_eval.fold_of), so a company is never scored with
what its own cited pages taught.

The measure: the share of answers whose validated number is on the 8 pages chosen, per attribute, next to
today's. A method qualifies when no attribute is more than 3 points worse than base. Counts and shares only
in the report (it is posted on a public repository); no reference answers.

Read-only. Env: SUPABASE_DB_URL, GOLD_TOKEN, ANTHROPIC_API_KEY (translate, locate); AIRTABLE_QA_TOKEN.
Options: --limit N per attribute, --methods, --budget minutes, --locate-usd, --attributes, --save FILE (with
learned: write the trained page choice for page_select.py, extract_eval --select learned).
"""
import argparse, collections, json, math, os, random, re, sys, time, traceback

import qa
import extract_eval as E
import rank_eval as R
import page_select as S
from value_eval import found_on, printed_forms, scales_for

METHODS = ("base", "expand", "doctype", "learned", "embed", "translate", "locate", "horizon")
N = E.MODEL_PAGES
BODY_KEEP = 6000                 # characters of each pool page kept in memory
LOCATE_POOL = 20                 # candidate pages the locate pass sees
LOCATE_CHARS = 450               # characters of extract per candidate page in the locate pass
EXTRA_LIMIT = 15                 # pages the translated words may add to the pool
EMBED_MODEL = S.EMBED_MODEL
RULE_POINTS = 3                  # a method qualifies when no attribute is more than this many points worse
NUMBER = re.compile(r"\d[\d.,]{2,}")


# ----- selection -----

def base_scored(pool):
    """Today's scored list for pages_for_model: [(score, top, lid, page, body)] in pool (rank) order."""
    return [((top[0] if top else -99) - 0.05 * i, top, lid, page, body) for i, (_, lid, page, body, top) in enumerate(pool)]


def choose(pool, rank_order=None, extra=None):
    """The 8 pages: the candidate half exactly as today, then the rank half in `rank_order` (indices into
    pool + extra) instead of today's order. Returns [(lid, page)]."""
    if rank_order is None:
        return [(lid, page) for _, _, lid, page, _ in E.pages_for_model(base_scored(pool), N)]
    scored = base_scored(list(pool) + list(extra or []))
    seen = set(rank_order)
    seq = [scored[i] for i in rank_order] + [x for j, x in enumerate(scored) if j not in seen]
    return [(lid, page) for _, _, lid, page, _ in E.pages_for_model(seq, N)]


HORIZON_PUSH = 20
TARGET_WORDS = re.compile(r"\b(targets?|goals?|aims?|ambitions?|commit(?:s|ted|ment|ments)?|pledges?|aspirations?|net[- ]zero|roadmap|"
                          r"by 20[3-5]\d|until 20[3-5]\d|reduce by|reduction of \d)\b", re.I)
YEARS = re.compile(r"\b(20\d\d)\b")


def future_heavy(body, year):
    """True when a page speaks more of the future than of the year asked: target words plus years three or more
    after `year`, against mentions of `year` and the year before (the actuals columns)."""
    ys = [int(y) for y in YEARS.findall(body or "")]
    now = sum(1 for y in ys if year - 1 <= y <= year)
    fut = sum(1 for y in ys if y >= year + 3) + len(TARGET_WORDS.findall(body or ""))
    return fut > now


def horizon_order(pool, year, push=HORIZON_PUSH):
    """Today's order with every future-heavy page moved `push` places down."""
    return sorted(range(len(pool)), key=lambda i: i + (push if future_heavy(pool[i][3], year) else 0))


def rrf(*orders, k=R.RRF_K):
    """Reciprocal rank fusion of orders (lists of indices): fused index order."""
    s = collections.defaultdict(float)
    for o in orders:
        for r, i in enumerate(o, 1):
            s[i] += 1.0 / (k + r)
    return sorted(s, key=lambda i: -s[i])


def by_score(scores):
    return sorted(range(len(scores)), key=lambda i: -scores[i])


# ----- learned pieces (from the other folds) -----

def expansion_terms(cases, attr, fold, query_words):
    other = [c for c in cases if c["attribute"] == attr and c["fold"] != fold and c["target"]]
    return R.expansion([{"attribute": attr, "marks": c["marks"]} for c in other], attr, set(query_words))


def type_lift(cases, attr, fold):
    """{doc_type: share among the cited pages / share among the pool pages} over the other folds."""
    tgt, pool = collections.Counter(), collections.Counter()
    for c in cases:
        if c["attribute"] != attr or c["fold"] == fold:
            continue
        for i, (_, lid, *_rest) in enumerate(c["pool"]):
            t = c["types"].get(lid, "")
            pool[t] += 1
            if i in c["target_idx"]:
                tgt[t] += 1
    nt, npool = sum(tgt.values()) or 1, sum(pool.values()) or 1
    return {t: ((tgt[t] + 0.5) / nt) / ((pool[t] + 0.5) / npool) for t in pool}


def expansion_scores(case, terms, idfs, avg_len):
    return S.expansion_scores(case["pool"], terms, idfs, avg_len)


def features(case, i, lift, exp, emb):
    return S.features(case["pool"], case["types"], i, lift, exp, emb)


# ----- meaning and language -----

Embedder = S.Embedder


def translations(attrs, langs, usage):
    """{(attribute, lang): [words]}: the words a report in that language prints for the attribute, one model
    call per attribute and language."""
    from model_reader import call
    out = {}
    for code, a in attrs.items():
        for lang in langs:
            text = call([{"role": "user", "content":
                          f"In company annual and sustainability reports written in the language with code '{lang}', which short "
                          f"labels are printed next to the figure for: {a.get('name')} ({'; '.join(a.get('queries', [])[:3])})? "
                          "Answer with a JSON list of 3 to 6 labels in that language, nothing else."}],
                        usage, system="You answer with JSON only.", max_tokens=200)
            try:
                words = [w for w in json.loads(re.search(r"\[.*\]", text, re.S).group(0)) if isinstance(w, str)]
            except Exception:
                words = []
            out[(code, lang)] = words[:6]
    return out


def ts_or(words):
    toks = [t for w in words for t in R.tokens(w) if len(t) > 2]
    return " | ".join(dict.fromkeys(toks))


def locate(case, attr, usage):
    """Indices (into pool + extra) the model picks, best first."""
    from model_reader import call
    items = list(case["pool"]) + list(case.get("extra", []))
    cand = list(range(min(len(items), LOCATE_POOL)))
    words = set(R.tokens(" ".join(attr.get("queries", []) + [attr.get("name", "")])))
    lines = []
    for i in cand:
        _, lid, page, body, _ = items[i]
        keep = [ln for ln in body.splitlines() if NUMBER.search(ln) and words & set(R.tokens(ln))] or body.splitlines()[:8]
        lines.append(f"[{i}] {case['types'].get(lid, '') or 'document'}, page {page}: " + " / ".join(keep)[:LOCATE_CHARS])
    text = call([{"role": "user", "content":
                  f"Which of these report pages most likely print the figure for: {attr.get('name')} for the year {case['year']}? "
                  f"Extracts:\n" + "\n".join(lines) + f"\n\nAnswer with a JSON list of up to {N} page numbers in brackets' "
                  "numbers, most likely first, nothing else."}],
                usage, system="You answer with JSON only.", max_tokens=100)
    try:
        got = [int(x) for x in json.loads(re.search(r"\[.*?\]", text, re.S).group(0)) if int(x) in cand]
    except Exception:
        got = []
    return list(dict.fromkeys(got))


# ----- the trained model (--save) -----

def save_model(cases, codes, rows_by_fold, idfs, avg_len):
    """What page_select.py needs to score pages: per attribute, the weights, type prior and expansion words
    trained without each fold and on all folds, and the idf of every expansion word. Counts and weights only."""
    out, words = {}, set()
    for code in codes:
        mine = [c for c in cases if c["attribute"] == code]
        if not mine:
            continue
        attr_cfg = {}
        qwords = R.query_terms(" ".join(SPEC_ATTRS.get(code, {}).get("queries", [])))
        for f in list(range(R.FOLDS)) + [None]:
            rows = [x for (at, ff), rs in rows_by_fold.items() if at == code and ff != f for x in rs]
            if not rows:
                continue
            terms = expansion_terms(cases, code, f, qwords)
            words |= set(terms)
            attr_cfg["all" if f is None else str(f)] = {
                "w": [round(x, 6) for x in R.train(rows, epochs=80)],
                "lift": {t: round(v, 6) for t, v in type_lift(cases, code, f).items()},
                "terms": terms, "rows": len(rows)}
        out[code] = attr_cfg
    return {"made_by": "select_eval.py --save", "made": time.strftime("%Y-%m-%d"), "answers": len(cases),
            "features": S.FEATURE_NAMES, "avg_len": round(avg_len, 3),
            "idf": {t: round(idfs.get(t, 0.0), 6) for t in sorted(words)}, "attributes": out}


SPEC_ATTRS = {}


# ----- the experiment -----

def gather(cur, g, attr, spec, known, trans):
    docs, weight = E.year_documents(cur, g["wba"], g["year"], attr)
    if not docs:
        return None
    ids = [d[0] for d in docs]
    from company_memory import memory_for
    memory = memory_for(cur, g["wba"], attr, g["year"], known, E.year_documents) if known else None
    rows = E.ranked_pages(cur, ids, weight, g["year"], attr, spec, {}, memory,
                          siblings=E.sibling_pages(cur, g["wba"], g["year"], attr, spec))
    pool = [(s, lid, page, (body or "")[:BODY_KEEP], top) for s, lid, page, body, top in E.keep_pages(rows)]
    forms = printed_forms(g["value"], scales_for(attr.get("unit_family")))
    has = lambda body: any(found_on(body, v) for v, _, _ in forms)
    cur.execute("select library_id, coalesce(lang, '') from public.documents where library_id = any(%s)", (ids,))
    langs = dict(cur.fetchall())
    extra = []
    foreign = sorted({l for l in langs.values() if l and l != "en"})
    for lang in foreign:
        q = ts_or(trans.get((g["attribute"], lang), []))
        if not q:
            continue
        lids = [l for l, x in langs.items() if x == lang]
        cur.execute("select library_id, page, body from public.pages where library_id = any(%s) and words @@ to_tsquery('simple', %s) "
                    "order by ts_rank_cd(words, to_tsquery('simple', %s), 32) desc limit %s", (lids, q, q, EXTRA_LIMIT))
        have = {(lid, page) for _, lid, page, _, _ in pool}
        extra += [(0.0, lid, page, (body or "")[:BODY_KEEP], None) for lid, page, body in cur.fetchall() if (lid, page) not in have]
    target = {i for i, (_, _, _, body, _) in enumerate(pool) if has(body)}
    extra_target = {len(pool) + i for i, (_, _, _, body, _) in enumerate(extra) if has(body)}
    marks = R.marking_terms({i: p[3] for i, p in enumerate(pool)}, target) if target else {}
    return {"wba": g["wba"], "attribute": g["attribute"], "year": g["year"], "fold": R.fold_of(g["wba"]),
            "pool": pool, "extra": extra, "target_idx": target, "target": target, "extra_target": extra_target,
            "types": {d[0]: d[1] for d in docs}, "langs": langs, "foreign": bool(foreign), "marks": marks}


def hit(case, chosen):
    items = list(case["pool"]) + list(case.get("extra", []))
    keys = {(items[i][1], items[i][2]) for i in case["target_idx"] | case["extra_target"]}
    return any(k in keys for k in chosen)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=150, help="answers per attribute (0 = all)")
    ap.add_argument("--attributes", default="")
    ap.add_argument("--methods", default=",".join(METHODS))
    ap.add_argument("--budget", type=float, default=70, help="minutes for reading the answers' pages")
    ap.add_argument("--locate-usd", type=float, default=2.0, help="cost limit of the locate and translate calls")
    ap.add_argument("--save", default="", help="with learned: write the trained page choice here (page_select.py reads it)")
    a = ap.parse_args()
    methods = [m for m in a.methods.split(",") if m in METHODS]
    if "base" not in methods:
        methods = ["base"] + methods
    run = qa.Run("select_eval.py")
    spec = json.load(open(E.SPEC, encoding="utf-8"))
    SPEC_ATTRS.update(spec["attributes"])
    gold_cfg = json.load(open(os.path.join(os.path.dirname(E.SPEC), "gold_attributes.json"), encoding="utf-8"))
    codes = [c for c, v in spec["attributes"].items() if v["type"] == "number" and (not a.attributes or c in a.attributes.split(","))]
    from value_eval import gold_values
    from model_reader import cost
    token = os.environ["GOLD_TOKEN"].strip()
    answers = [g for base in gold_cfg["bases"].values() for g in gold_values(token, base, codes)
               if g["value"] and not g["unit"].lower().startswith(("%", "percent"))]
    gold_index = {(g["wba"], g["attribute"], g["year"]): g["value"] for g in answers}
    by = collections.defaultdict(list)
    for g in answers:
        by[g["attribute"]].append(g)
    queues = [by[c][:a.limit] if a.limit else by[c] for c in codes]
    todo = [q[i] for i in range(max((len(q) for q in queues), default=0)) for q in queues if i < len(q)]
    print(f">>> {len(todo):,} answers; methods {', '.join(methods)}", flush=True)
    usage = collections.Counter()
    trans = {}
    t0 = time.time()
    cases, failed = [], 0
    with __import__("evidence_db").connect() as conn:
        cur = conn.cursor()
        cur.execute("set statement_timeout = '30s'")
        if "translate" in methods and os.environ.get("ANTHROPIC_API_KEY"):
            cur.execute("select lang, count(*) from public.documents where lang <> '' and lang <> 'en' group by lang order by 2 desc limit 8")
            langs = [l for l, _ in cur.fetchall()]
            trans = translations({c: spec["attributes"][c] for c in codes}, langs, usage)
            print(f">>> translations for {len(langs)} languages: {', '.join(langs)}", flush=True)
        for i, g in enumerate(todo, 1):
            if time.time() - t0 > a.budget * 60:
                print(f">>> stopped reading at the {a.budget:g}-minute budget, {i - 1:,} answers", flush=True); break
            if i % 100 == 0:
                print(f"  {i:,}/{len(todo):,}, {len(cases):,} with pages, {(time.time() - t0) / 60:.0f} min", flush=True)
            known = {y: v for (w, c, y), v in gold_index.items() if w == g["wba"] and c == g["attribute"] and y != g["year"]}
            try:
                c = gather(cur, g, spec["attributes"][g["attribute"]], spec, known, trans)
                failed = 0
            except Exception as e:
                failed += 1
                conn.rollback(); cur.execute("set statement_timeout = '30s'")
                if failed >= E.DB_FAILS_IN_A_ROW:
                    raise RuntimeError(f"the evidence database did not answer {failed} times in a row ({type(e).__name__})")
                continue
            if c:
                cases.append(c)
    print(f">>> {len(cases):,} answers with pages, read in {(time.time() - t0) / 60:.0f} min", flush=True)

    # BM25 statistics over every pool page
    all_pages = [p[3] for c in cases for p in c["pool"]]
    df = collections.Counter(t for body in all_pages for t in set(R.tokens(body)))
    idfs = {t: R.idf(n, len(all_pages)) for t, n in df.items()}
    avg_len = (sum(len(set(R.tokens(b))) for b in all_pages) / len(all_pages)) if all_pages else 1
    emb = None
    if "embed" in methods or "learned" in methods:
        try:
            emb = Embedder()
        except Exception as e:
            print(f">>> embeddings unavailable ({type(e).__name__}: {str(e)[:100]}); embed left out", flush=True)
            methods = [m for m in methods if m != "embed"]
    results = {m: collections.Counter() for m in methods}
    lift_cache, exp_cache = {}, {}
    for c in cases:
        attr = spec["attributes"][c["attribute"]]
        n_pool = len(c["pool"])
        key = (c["attribute"], c["fold"])
        if key not in lift_cache:
            lift_cache[key] = type_lift(cases, c["attribute"], c["fold"])
            qwords = R.query_terms(" ".join(attr.get("queries", [])))
            exp_cache[key] = expansion_terms(cases, c["attribute"], c["fold"], qwords)
        lift, terms = lift_cache[key], exp_cache[key]
        exp = expansion_scores(c, terms, idfs, avg_len)
        sims = None
        if emb and n_pool:
            sims = emb.sims(f"{attr.get('name')}: {'; '.join(attr.get('queries', [])[:3])}", [p[3] for p in c["pool"]])
        c["_exp"], c["_sims"], c["_lift"] = exp, sims, lift
        base_order = list(range(n_pool))
        orders = {"base": None}
        if "expand" in methods:
            orders["expand"] = rrf(base_order, by_score(exp)) if terms else base_order
        if "doctype" in methods:
            orders["doctype"] = rrf(base_order, by_score([lift.get(c["types"].get(p[1], ""), 1.0) for p in c["pool"]]))
        if "embed" in methods and sims:
            orders["embed"] = rrf(base_order, by_score(sims))
        if "horizon" in methods:
            orders["horizon"] = horizon_order(c["pool"], c["year"])
            r = results["horizon"]
            r["future-heavy pages"] += sum(future_heavy(p[3], c["year"]) for p in c["pool"])
            r["pool pages"] += n_pool
        for m in methods:
            r = results[m]
            if m == "learned" or m == "locate" or m == "translate":
                continue
            r["answers"] += 1
            r[f"answers {c['attribute']}"] += 1
            ok = hit(c, choose(c["pool"], orders.get(m)))
            r["hit"] += ok
            r[f"hit {c['attribute']}"] += ok
    if "learned" in methods:
        rows_by_fold = collections.defaultdict(list)
        for c in cases:
            neg = [i for i in range(len(c["pool"])) if i not in c["target_idx"]]
            keep = sorted(c["target_idx"]) + random.Random(c["wba"]).sample(neg, min(len(neg), 8))   # all cited, 8 others
            for i in keep:
                rows_by_fold[(c["attribute"], c["fold"])].append(
                    (features(c, i, c["_lift"], c["_exp"], c["_sims"]), 1 if i in c["target_idx"] else 0))
        weights = {}
        for c in cases:
            key = (c["attribute"], c["fold"])
            if key not in weights:
                rows = [x for (at, f), rs in rows_by_fold.items() if at == c["attribute"] and f != c["fold"] for x in rs]
                weights[key] = R.train(rows, epochs=80) if rows else None
            w = weights[key]
            order_ = by_score([R.score(w, features(c, i, c["_lift"], c["_exp"], c["_sims"])) for i in range(len(c["pool"]))]) if w else None
            r = results["learned"]
            r["answers"] += 1; r[f"answers {c['attribute']}"] += 1
            ok = hit(c, choose(c["pool"], order_))
            r["hit"] += ok; r[f"hit {c['attribute']}"] += ok
        if a.save:
            model = save_model(cases, codes, rows_by_fold, idfs, avg_len)
            with open(a.save, "w", encoding="utf-8") as f:
                json.dump(model, f, indent=1, sort_keys=True)
            print(f">>> trained page choice written to {a.save} ({len(model['attributes'])} attributes)", flush=True)
            # Run artifacts cannot be fetched from every place the file is needed (6 Nov 2026), so the model is also
            # printed, between markers, for a person or a session to copy into the repository. Weights, document
            # types and words only: no company and no answer.
            print(">>> SELECT_LEARNED_JSON_BEGIN\n" + json.dumps(model, sort_keys=True) + "\n>>> SELECT_LEARNED_JSON_END", flush=True)
    if "translate" in methods:
        for c in cases:
            r = results["translate"]
            r["answers"] += 1; r[f"answers {c['attribute']}"] += 1
            if c["extra"]:
                n_pool = len(c["pool"])
                order_ = rrf(list(range(n_pool)), list(range(n_pool, n_pool + len(c["extra"]))))
                ok = hit(c, choose(c["pool"], order_, c["extra"]))
                r["foreign answers"] += 1
            else:
                ok = hit(c, choose(c["pool"]))
            r["hit"] += ok; r[f"hit {c['attribute']}"] += ok
    if "locate" in methods and os.environ.get("ANTHROPIC_API_KEY"):
        rng = random.Random(5)
        sample = list(cases); rng.shuffle(sample)
        r = results["locate"]
        for c in sample:
            if cost(usage) >= a.locate_usd:
                r["stopped at the cost limit"] = 1; break
            picked = locate(c, spec["attributes"][c["attribute"]], usage)
            items = list(c["pool"]) + list(c.get("extra", []))
            chosen = [(items[i][1], items[i][2]) for i in picked][:N]
            for lid, page in choose(c["pool"]):              # fill from today's choice when the model picked fewer
                if len(chosen) >= N:
                    break
                if (lid, page) not in chosen:
                    chosen.append((lid, page))
            ok = hit(c, chosen)
            r["answers"] += 1; r[f"answers {c['attribute']}"] += 1
            r["hit"] += ok; r[f"hit {c['attribute']}"] += ok
            b = results.setdefault("base on the locate sample", collections.Counter())
            okb = hit(c, choose(c["pool"]))
            b["answers"] += 1; b[f"answers {c['attribute']}"] += 1; b["hit"] += okb; b[f"hit {c['attribute']}"] += okb
    md = report(cases, results, codes, spec, usage, cost(usage))
    open("select_eval.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    run.finish("ok", rows_in=len(todo), rows_out=len(cases), counters={m: dict(v) for m, v in results.items()})
    return 0


def share(r, code=None):
    n = r[f"answers {code}"] if code else r["answers"]
    h = r[f"hit {code}"] if code else r["hit"]
    return (h / n) if n else None


def report(cases, results, codes, spec, usage, usd):
    pool_has = collections.Counter(); answers = collections.Counter()
    for c in cases:
        answers[c["attribute"]] += 1
        pool_has[c["attribute"]] += bool(c["target_idx"])
    methods = [m for m in results if results[m]["answers"]]
    md = ["### Choosing the model reader's 8 pages: six ideas against today's", "",
          f"{len(cases):,} validated answers with pages. Share whose validated number is on the 8 pages chosen; "
          "\"in the pool\" is the most any choice from today's 25 ranked pages can reach. Methods learn from the other "
          "companies only (by fold). Locate is scored on its own sample, next to base on that same sample. "
          f"Model calls (translate, locate): {usage['calls']}, about ${usd:.2f}.", "",
          "| Attribute | Answers | In the pool | " + " | ".join(methods) + " |",
          "| --- | --: | --: |" + " --: |" * len(methods)]
    fmt = lambda x: "" if x is None else f"{x:.0%}"
    for code in codes:
        if not answers[code]:
            continue
        md.append(f"| {code} {spec['attributes'][code].get('name', '')[:40]} | {answers[code]:,} | {pool_has[code] / answers[code]:.0%} | "
                  + " | ".join(fmt(share(results[m], code)) for m in methods) + " |")
    n = len(cases) or 1
    md.append(f"| All | {len(cases):,} | {sum(pool_has.values()) / n:.0%} | " + " | ".join(fmt(share(results[m])) for m in methods) + " |")
    md += ["", f"Qualifies (no attribute more than {RULE_POINTS} points under base on the same answers):", ""]
    for m in methods:
        if m.startswith("base"):
            continue
        ref = results["base on the locate sample"] if m == "locate" else results["base"]
        worst = min(((share(results[m], c) or 0) - (share(ref, c) or 0) for c in codes if results[m][f"answers {c}"]), default=0)
        gain = (share(results[m]) or 0) - (share(ref) or 0)
        md.append(f"- {m}: {gain * 100:+.1f} points overall, worst attribute {worst * 100:+.1f}: "
                  + ("qualifies" if worst >= -RULE_POINTS / 100 else "does not qualify"))
    if results.get("translate", {}).get("foreign answers"):
        md.append(f"\nTranslate changed the pool for {results['translate']['foreign answers']:,} answers (documents not in English).")
    h = results.get("horizon", {})
    if h.get("pool pages"):
        md.append(f"\nHorizon: {h['future-heavy pages']:,} of {h['pool pages']:,} pool pages ({h['future-heavy pages'] / h['pool pages']:.0%}) speak more of the future than of the year asked.")
    return md


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("select_eval.md", "w").write("### Page choice experiment: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
