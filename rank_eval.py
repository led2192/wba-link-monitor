#!/usr/bin/env python3
"""
rank_eval.py: would a better page ranking find the page the analyst cited more often?

An offline experiment (proposals 1 and 2 of the information retrieval review, 4 Nov 2026). It takes the
same validated answers as gold_eval.py (a quote, a source, the cited document in the library, its text in
the evidence database, and the quote found in that text, so the cited page is known in our numbering) and
ranks every page of the cited document eleven ways:
  ts best-of     today's ts_rank_cd, best rank over the attribute's alternative queries (what gold_eval.py
                 reports; generous, as it picks the luckiest query after the fact);
  ts single      ts_rank_cd of one query holding all the alternatives (one list, as a reader would get);
  bm25 single    Okapi BM25 over all the attributes' query words, with idf from a sample of public.pages;
  bm25 best-of   BM25 per alternative query, best rank over them;
  ts fused,      each alternative query ranked on its own, the lists merged by reciprocal rank fusion (a page
  bm25 fused     several queries put high comes first): one list, with no hindsight about which query fits;
  zones single,  BM25F: a query word in a table row or a heading counts more than in running text (weights in
  zones fused    ZONE_WEIGHTS, by attribute kind), as one query and fused over the alternatives;
  expanded       BM25 with words added to the query that mark the cited page in other companies' reports
  single, fused  (learned per attribute on the other folds' cited pages, weight 0.3), as one query and fused;
  learned        a logistic regression over page features (both scores, word coverage, phrase hits, labels at a
                 line start, number density, the year, position in the document), trained on the cited pages
                 of the other companies (five folds split by company, so no company scores its own pages).
It reports top 1, top 3, top 10 and mean reciprocal rank per attribute and method. No model calls, no
writes to the library or the evidence database. Changes nothing in production: if a method wins, a later
pull request moves it into the readers.

Dry run by default: --limit answers per attribute (20 unless given), results printed and in rank_eval.md;
--commit records the run and one evals row per method in Pipeline QA. Env: GOLD_TOKEN, AIRTABLE_TOKEN,
AIRTABLE_BASE, SUPABASE_DB_URL; AIRTABLE_QA_TOKEN (optional).
"""
import argparse, collections, json, math, os, random, re, sys, time, traceback, zlib

import qa
import gold_eval as G

K1, B = 1.2, 0.75               # BM25 constants, the textbook defaults
SAMPLE_PCT = 1                  # share of public.pages sampled for document frequencies
FOLDS = 5
NEGATIVES = 20                  # training pages per answer that are not cited: the 12 best by BM25 and 8 at random
EPOCHS, RATE, L2 = 200, 0.5, 1e-3
RRF_K = 60                      # reciprocal rank fusion constant, the usual default
METHODS = ["ts best-of", "ts single", "ts fused", "bm25 single", "bm25 best-of", "bm25 fused", "zones single",
           "zones fused", "expanded single", "expanded fused", "learned"]
# Query expansion (proposal 4): words that mark the cited page within its document, learned per attribute from
# the other companies' cited pages (same folds as the learned ranker), added to the query at a lower weight.
EXPAND_TERMS, EXPAND_WEIGHT, EXPAND_SUPPORT = 10, 0.3, 3
# Zone weights for BM25F (proposal 3): a query word counts this many times when it sits in a table row (a short
# line holding numbers), a heading (a short line with no closing punctuation) or running text. Numbers live in
# table rows; who is responsible for what is announced in headings ("Board oversight of climate").
ZONE_WEIGHTS = {"numeric": {"table": 3.0, "heading": 1.5, "prose": 1.0},
                "narrative": {"table": 1.0, "heading": 2.5, "prose": 1.0}}
FEATURES = ["bm25 share", "bm25 rank", "ts share", "ts rank", "coverage", "phrases", "line labels", "numbers",
            "numbers x numeric", "year", "position", "length", "fused rank", "in table rows", "in headings"]
NUMERIC = {"GMC_00001.1", "GMC_00002.1", "GMC_00003.1", "GMC_00048.1", "GMC_02526.1", "GMC_00064.1"}
WORD = re.compile(r"[^\W_]+")
NUM = re.compile(r"^\d[\d.,]*$")


# ----------------------------------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------------------------------
def tokens(text):
    """The words the 'simple' configuration indexes: runs of letters and digits, lower case."""
    return WORD.findall((text or "").lower())


def query_terms(q):
    """'"scope 1" emissions tCO2e' -> ['scope', '1', 'emissions', 'tco2e'] (stop words out, order kept, no repeats)."""
    return list(dict.fromkeys(w for w in tokens(q.replace('"', " ")) if w not in G.STOP))


def query_phrases(q):
    """The quoted phrases of a query, as word lists: '"scope 1" "scope 2" emissions' -> [['scope', '1'], ['scope', '2']]."""
    return [tokens(p) for p in re.findall(r'"([^"]+)"', q or "") if tokens(p)]


def idf(df, n):
    """BM25 idf with the +1 inside the log, so a word on more than half the pages still counts a little."""
    return math.log(1 + (n - df + 0.5) / (df + 0.5))


def bm25(tf, length, avg_len, terms, idfs, k1=K1, b=B):
    """Okapi BM25 of one page: tf is a Counter of the page's words. Length is the number of distinct words, the
    same measure as length(words) in the database, from which the average comes."""
    s = 0.0
    norm = k1 * (1 - b + b * length / (avg_len or 1))
    for t in terms:
        f = tf.get(t, 0)
        if f:
            s += idfs.get(t, 0.0) * f * (k1 + 1) / (f + norm)
    return s


def has_phrase(toks, phrase):
    n = len(phrase)
    return any(toks[i:i + n] == phrase for i in range(len(toks) - n + 1)) if n else False


def line_labels(body, terms):
    """How many lines start with one of the query words (within the first three words of the line): a table
    row or a heading carries its label at the start, a passing mention sits mid-sentence."""
    ts, n = set(terms), 0
    for line in (body or "").splitlines():
        if ts & set(tokens(line)[:3]):
            n += 1
    return n


def order(scores):
    """Pages by score, best first; ties by page number (as the database orders them)."""
    return [p for p, _ in sorted(scores.items(), key=lambda t: (-t[1], t[0]))]


def recip_ranks(scores):
    return {p: 1.0 / i for i, p in enumerate(order(scores), 1)}


def line_zone(line):
    """'table', 'heading' or 'prose' for one line of page text (None for an empty line)."""
    toks = tokens(line)
    if not toks:
        return None
    nums = sum(1 for w in toks if NUM.match(w))
    if nums and len(toks) <= 30 and nums / len(toks) >= 0.15:
        return "table"
    if len(toks) <= 12 and not line.rstrip().endswith((".", ";", ",", ":")):
        return "heading"
    return "prose"


def zone_tf(body):
    """{zone: Counter of words} for one page."""
    out = {"table": collections.Counter(), "heading": collections.Counter(), "prose": collections.Counter()}
    for line in (body or "").splitlines():
        z = line_zone(line)
        if z:
            out[z].update(tokens(line))
    return out


def bm25f(ztf, length, avg_len, terms, idfs, weights, k1=K1, b=B):
    """BM25F: each word's frequency is the zone-weighted sum over the zones, then saturated once, so a word
    repeated in a table row counts more than in running text without one zone running away."""
    s = 0.0
    norm = k1 * (1 - b + b * length / (avg_len or 1))
    for t in terms:
        f = sum(w * ztf[z].get(t, 0) for z, w in weights.items())
        if f:
            s += idfs.get(t, 0.0) * f * (k1 + 1) / (f + norm)
    return s


def zone_scores(pages, attr, queries, idfs, avg_len):
    """({page: BM25F of all the query words}, [{page: BM25F} per alternative query])."""
    w = ZONE_WEIGHTS["numeric" if attr in NUMERIC else "narrative"]
    terms = list(dict.fromkeys(t for q in queries for t in query_terms(q)))
    single, per = {}, [{} for _ in queries]
    for p, body in pages.items():
        ztf = zone_tf(body)
        length = len(set(tokens(body)))
        single[p] = bm25f(ztf, length, avg_len, terms, idfs, w)
        for i, q in enumerate(queries):
            per[i][p] = bm25f(ztf, length, avg_len, query_terms(q), idfs, w)
    return single, per


def marking_terms(pages, target):
    """{word: how well it marks the cited pages of this document}: for each word on a cited page, 1 minus the
    share of the document's pages that hold it. A word on every page scores 0; a word only on the cited page
    scores close to 1. Numbers and stop words are left out (they do not carry over to another company)."""
    sets = {p: set(tokens(b)) for p, b in pages.items()}
    n = len(sets) or 1
    cited = set().union(*(sets[p] for p in target if p in sets)) if target else set()
    out = {}
    for w in cited:
        if len(w) < 3 or w in G.STOP or not w.isalpha():
            continue
        out[w] = 1 - sum(1 for s in sets.values() if w in s) / n
    return out


def expansion(cases, attr, exclude, k=EXPAND_TERMS, support=EXPAND_SUPPORT):
    """The k words that best mark the cited page across these cases of one attribute (their marking scores
    summed), found on the cited pages of at least `support` cases and not already in the query."""
    total, seen = collections.Counter(), collections.Counter()
    for c in cases:
        if c["attribute"] != attr:
            continue
        for w, v in c["marks"].items():
            total[w] += v; seen[w] += 1
    ranked = [w for w, _ in total.most_common() if seen[w] >= support and w not in exclude]
    return ranked[:k]


def bm25_weighted(tf, length, avg_len, weights, idfs, k1=K1, b=B):
    """BM25 with a weight per query word (1 for the query's own words, EXPAND_WEIGHT for added ones)."""
    s = 0.0
    norm = k1 * (1 - b + b * length / (avg_len or 1))
    for t, wt in weights.items():
        f = tf.get(t, 0)
        if f:
            s += wt * idfs.get(t, 0.0) * f * (k1 + 1) / (f + norm)
    return s


def fuse(rankings, k=RRF_K):
    """Reciprocal rank fusion: each ranking (a dict of page scores; pages scoring 0 left out) gives a page
    1 / (k + its rank), summed over the rankings. A page that several alternative queries rank high comes
    first, without having to know beforehand which query suits this company's wording."""
    fused = collections.defaultdict(float)
    for sc in rankings:
        for i, p in enumerate(order({p: v for p, v in sc.items() if v > 0}), 1):
            fused[p] += 1.0 / (k + i)
    return dict(fused)


def page_features(pages, attr, year, queries, idfs, avg_len, ts):
    """({page: [features]}, {page: BM25 of all the query words}, [{page: BM25} per alternative query]) for every
    page of one document. pages: {page: body}; ts: {page: ts_rank_cd of the single query} (absent when the
    page does not match)."""
    terms = list(dict.fromkeys(t for q in queries for t in query_terms(q)))
    phrases = [p for q in queries for p in query_phrases(q)]
    toks = {p: tokens(b) for p, b in pages.items()}
    bm, per_bm = {}, [{} for _ in queries]
    for p, t in toks.items():
        tf = collections.Counter(t)
        bm[p] = bm25(tf, len(tf), avg_len, terms, idfs)
        for i, q in enumerate(queries):
            per_bm[i][p] = bm25(tf, len(tf), avg_len, query_terms(q), idfs)
    fused_r = recip_ranks(fuse(per_bm))
    tsa = {p: ts.get(p, 0.0) for p in pages}
    bm_max, ts_max = max(bm.values(), default=0) or 1, max(tsa.values(), default=0) or 1
    bm_r, ts_r = recip_ranks(bm), recip_ranks(tsa)
    last = max(pages, default=1) or 1
    out = {}
    for p, t in toks.items():
        s = set(t)
        ztf = zone_tf(pages[p])
        hits = sum(1 for z in ztf for w in terms if ztf[z].get(w)) or 1
        nums = sum(1 for w in t if NUM.match(w))
        dens = nums / (len(t) or 1)
        out[p] = [bm[p] / bm_max, bm_r[p], tsa[p] / ts_max, ts_r[p] if tsa[p] else 0.0,
                  len(s & set(terms)) / (len(terms) or 1), sum(has_phrase(t, ph) for ph in phrases) / (len(phrases) or 1),
                  min(line_labels(pages[p], terms), 10) / 10, min(dens * 5, 1.0), min(dens * 5, 1.0) * (attr in NUMERIC),
                  float(str(year) in s or str(year - 1) in s), p / last, min(math.log1p(len(t)) / 8, 1.0),
                  fused_r.get(p, 0.0),
                  sum(1 for w in terms if ztf["table"].get(w)) / hits, sum(1 for w in terms if ztf["heading"].get(w)) / hits]
    return out, bm, per_bm


def best_of(per_query_scores, target):
    """Best rank of the target over several rankings (dicts of page scores), or None."""
    best = None
    for sc in per_query_scores:
        rk = G.rank_of(target, order({p: s for p, s in sc.items() if s > 0}))
        if rk and (best is None or rk < best):
            best = rk
    return best


def fold_of(wba, folds=FOLDS):
    return zlib.crc32((wba or "").encode()) % folds


def sigmoid(z):
    return 1 / (1 + math.exp(-max(min(z, 30), -30)))


def train(rows, epochs=EPOCHS, rate=RATE, l2=L2):
    """Logistic regression by batch gradient descent, cited pages weighted up to balance the classes.
    rows: [(features, 0 or 1)]. Returns the weights, bias last. Plain Python: a few thousand rows."""
    if not rows:
        return [0.0] * (len(FEATURES) + 1)
    d = len(rows[0][0])
    w = [0.0] * (d + 1)
    pos = sum(y for _, y in rows) or 1
    wpos = (len(rows) - pos) / pos
    for _ in range(epochs):
        g = [0.0] * (d + 1)
        for x, y in rows:
            e = (sigmoid(sum(a * b for a, b in zip(w, x)) + w[-1]) - y) * (wpos if y else 1.0)
            for j in range(d):
                g[j] += e * x[j]
            g[-1] += e
        n = len(rows)
        for j in range(d):
            w[j] -= rate * (g[j] / n + l2 * w[j])
        w[-1] -= rate * g[-1] / n
    return w


def score(w, x):
    return sum(a * b for a, b in zip(w, x)) + w[-1]


def training_rows(case, rng):
    """The cited pages and NEGATIVES others of one answer: the best by BM25 (the hard ones) and some at random."""
    pos = [p for p in case["features"] if p in case["target"]]
    neg = [p for p in order(case["bm25"]) if p not in case["target"]]
    hard, rest = neg[:12], neg[12:]
    pick = hard + rng.sample(rest, min(len(rest), NEGATIVES - len(hard)))
    return [(case["features"][p], 1) for p in pos] + [(case["features"][p], 0) for p in pick]


def cross_validate(cases, folds=FOLDS, seed=7):
    """Sets case['ranks']['learned'] for every case from a model trained on the other folds' companies.
    Returns the weights of a model trained on all cases (for the report)."""
    rng = random.Random(seed)
    rows_by_fold = collections.defaultdict(list)
    for c in cases:
        rows_by_fold[fold_of(c["wba"], folds)] += training_rows(c, rng)
    for f in range(folds):
        test = [c for c in cases if fold_of(c["wba"], folds) == f]
        if not test:
            continue
        w = train([r for g, rs in rows_by_fold.items() if g != f for r in rs])
        for c in test:
            c["ranks"]["learned"] = G.rank_of(c["target"], order({p: score(w, x) for p, x in c["features"].items()}))
    return train([r for rs in rows_by_fold.values() for r in rs])


def metrics(ranks):
    """{'n', 'top 1', 'top 3', 'top 10', 'mrr'} from a list of ranks (None = not found)."""
    n = len(ranks) or 1
    out = {"n": len(ranks)}
    for k in (1, 3, 10):
        out[f"top {k}"] = sum(1 for r in ranks if r and r <= k) / n
    out["mrr"] = sum(1 / r for r in ranks if r) / n
    return out


# ----------------------------------------------------------------------------------------------
# Evidence database (read only)
# ----------------------------------------------------------------------------------------------
STATS_CHUNK = 40                # words counted per pass over the sample (4 Nov 2026: 550 words in one pass hit
                                # the 300-second limit after 100 minutes of reading, and the run was lost)


def collection_stats(cur, terms, pct=SAMPLE_PCT, chunk=STATS_CHUNK, deadline=None):
    """(pages in the sample, {term: pages holding it}, average page length in words) from a repeatable
    sample of public.pages, counting up to `chunk` words per pass. A pass the database cancels leaves its
    words out (no idf: they count for nothing) instead of failing the run; the connection is rolled back
    so the run goes on. Past `deadline` (a time.time() value) the remaining passes are skipped the same way."""
    terms = sorted(set(terms))
    n, avg, dfs = 0, 0.0, {}
    for i in range(0, max(len(terms), 1), chunk):
        part = terms[i:i + chunk]
        if deadline and time.time() > deadline:
            print(f"  word counts for the last {len(terms) - i} words skipped (time); they are left out", flush=True)
            break
        cols = ", ".join(f"count(*) filter (where words @@ to_tsquery('simple', %s))" for _ in part)
        try:
            cur.execute(f"select count(*), avg(length(words)){', ' + cols if cols else ''} "
                        f"from public.pages tablesample system ({pct}) repeatable (42)", part)
            r = cur.fetchone()
        except Exception as e:
            print(f"  word counts for {len(part)} words not read ({type(e).__name__}); they are left out", flush=True)
            cur.connection.rollback()
            continue
        n, avg = n or (r[0] or 1), avg or float(r[1] or 0)
        dfs.update({t: r[2 + j] for j, t in enumerate(part)})
    return n or 1, dfs, avg


def doc_pages(cur, lid, single, ors):
    """{page: body}, {page: ts_rank_cd of the single query}, [{page: ts_rank_cd} per alternative query] for one
    document, in one statement (a page that does not match a query has no entry for it)."""
    qs = [single] + list(ors)
    cols = ", ".join(f"case when words @@ q{i} then ts_rank_cd(words, q{i}) end" for i in range(len(qs)))
    froms = ", ".join(f"websearch_to_tsquery('simple', %s) q{i}" for i in range(len(qs)))
    cur.execute(f"select page, body, {cols} from public.pages, {froms} where library_id = %s order by page", qs + [lid])
    pages, ts = {}, [{} for _ in qs]
    for r in cur.fetchall():
        pages[r[0]] = r[1] or ""
        for i in range(len(qs)):
            if r[2 + i] is not None:
                ts[i][r[0]] = float(r[2 + i])
    return pages, ts[0], ts[1:]


# ----------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="answers per attribute (dry run default 20; 0 = all)")
    ap.add_argument("--attributes", default="", help="comma-separated GMC codes (default: all in gold_attributes.json)")
    ap.add_argument("--budget", type=int, default=100, help="minutes for reading documents; then it scores what it has")
    ap.add_argument("--post-budget", type=int, default=15, help="minutes for the expansion words' counts after the read")
    a = ap.parse_args()
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    limit = a.limit or (0 if a.commit else 20)
    cfg = json.load(open(G.CONFIG, encoding="utf-8"))
    codes = [c for c in cfg["attributes"] if not a.attributes or c in a.attributes.split(",")]
    gold_token = os.environ.get("GOLD_TOKEN", "").strip()
    if not gold_token:
        open("rank_eval.md", "w").write("### Ranking experiment: GOLD_TOKEN is not set; nothing run.\n")
        return 0
    run = qa.Run("rank_eval.py")
    import psycopg

    answers = []
    for name, base in cfg["bases"].items():
        answers += G.gold_answers(gold_token, base, codes)
    by_attr = collections.defaultdict(list)
    for g in answers:
        by_attr[g["attribute"]].append(g)
    queues = [by_attr[c][:limit] if limit else by_attr[c] for c in codes]
    answers = [q[i] for i in range(max((len(q) for q in queues), default=0)) for q in queues if i < len(q)]
    rows = G.library_rows(os.environ["AIRTABLE_TOKEN"], os.environ["AIRTABLE_BASE"], [g["wba"] for g in answers])
    print(f">>> {len(answers):,} validated answers, {len(rows):,} library rows", flush=True)

    all_terms = {t for c in codes for q in cfg["attributes"][c]["queries"] for t in query_terms(q)}
    cases, skipped = [], collections.Counter()
    t0, cut = time.time(), False
    with __import__("evidence_db").connect() as conn:
        cur = conn.cursor()
        cur.execute("set statement_timeout = '300s'")
        n_sample, dfs, avg_len = collection_stats(cur, all_terms)
        idfs = {t: idf(dfs.get(t, 0), n_sample) for t in all_terms}
        print(f">>> idf from {n_sample:,} sampled pages, average {avg_len:.0f} distinct words a page", flush=True)
        cur.execute("set statement_timeout = '60s'")
        for i, g in enumerate(answers, 1):
            if time.time() - t0 > a.budget * 60:
                cut = True
                break
            if i % 50 == 0:
                print(f"  {i:,}/{len(answers):,} answers, {len(cases):,} usable, {(time.time() - t0) / 60:.0f} min", flush=True)
            pairs = G.match(g["sources"], rows)
            lid = pairs[0][1]["library_id"] if pairs else None
            if not lid:
                skipped["source not in the library"] += 1; continue
            queries = cfg["attributes"][g["attribute"]]["queries"]
            ors = [G.or_query(q) for q in queries]
            try:
                n_pages, found, _ = G.doc_scores(cur, lid, G.quote_phrases(g["quote"]), [])
                if not n_pages:
                    skipped["no text in the database"] += 1; continue
                if not any(found):
                    skipped["quote not found in our text"] += 1; continue
                pages, ts, per_ts = doc_pages(cur, lid, " or ".join(ors), ors)
            except Exception as e:
                conn.rollback(); cur.execute("set statement_timeout = '60s'")
                skipped[f"database did not answer ({type(e).__name__})"] += 1; continue
            target = G.locate(found, g["pages"]) & set(pages)
            if not target:
                skipped["cited page not located"] += 1; continue
            feats, bm, per_bm = page_features(pages, g["attribute"], g["year"] or 0, queries, idfs, avg_len, ts)
            zs, per_zs = zone_scores(pages, g["attribute"], queries, idfs, avg_len)
            cases.append({"attribute": g["attribute"], "wba": g["wba"], "id": g["id"], "target": target, "features": feats,
                          "bm25": bm, "per_bm": per_bm, "marks": marking_terms(pages, target),
                          "pages_z": zlib.compress(json.dumps(pages).encode()), "ranks": {
                              "ts best-of": best_of(per_ts, target),
                              "ts single": G.rank_of(target, order(ts)),
                              "ts fused": G.rank_of(target, order(fuse(per_ts))),
                              "bm25 single": G.rank_of(target, order(bm)),
                              "bm25 best-of": best_of(per_bm, target),
                              "bm25 fused": G.rank_of(target, order(fuse(per_bm))),
                              "zones single": G.rank_of(target, order(zs)),
                              "zones fused": G.rank_of(target, order(fuse(per_zs)))}})
        # query expansion, per fold so no company's own cited page shapes its query
        expanded_by = {}
        for f in range(FOLDS):
            train_cases = [c for c in cases if fold_of(c["wba"]) != f]
            for code in codes:
                base = {t for q in cfg["attributes"][code]["queries"] for t in query_terms(q)}
                expanded_by[(f, code)] = expansion(train_cases, code, base)
        everyone = {code: expansion(cases, code, {t for q in cfg["attributes"][code]["queries"] for t in query_terms(q)})
                    for code in codes}
        new_terms = {t for ts in list(expanded_by.values()) + list(everyone.values()) for t in ts} - set(idfs)
        print(f">>> {len(cases):,} usable answers read in {(time.time() - t0) / 60:.0f} min; word counts for "
              f"{len(new_terms):,} expansion words", flush=True)
        if new_terms:
            # 4 Nov 2026: this pass ran unlogged for 50 minutes after a 100-minute read and the job hit its
            # timeout, losing the whole run. It now has its own deadline; words it cannot count are dropped
            # from the expansions, so no expansion word scores without an idf.
            cur.execute("set statement_timeout = '120s'")
            _, dfs2, _ = collection_stats(cur, new_terms, deadline=time.time() + a.post_budget * 60)
            idfs.update({t: idf(dfs2[t], n_sample) for t in new_terms if t in dfs2})
            dropped = {t for t in new_terms if t not in dfs2}
            if dropped:
                print(f"  {len(dropped):,} expansion words without a count are left out", flush=True)
                expanded_by = {k: [t for t in v if t not in dropped] for k, v in expanded_by.items()}
                everyone = {k: [t for t in v if t not in dropped] for k, v in everyone.items()}
    for c in cases:
        extra = expanded_by.get((fold_of(c["wba"]), c["attribute"]), [])
        base = {t for q in cfg["attributes"][c["attribute"]]["queries"] for t in query_terms(q)}
        wts = {**{t: EXPAND_WEIGHT for t in extra}, **{t: 1.0 for t in base}}
        pages = {int(k): v for k, v in json.loads(zlib.decompress(c.pop("pages_z"))).items()}
        tfs = {p: collections.Counter(tokens(body)) for p, body in pages.items()}       # once per page, not twice
        exp = {p: bm25_weighted(tf, len(tf), avg_len, wts, idfs) for p, tf in tfs.items()}
        only_extra = {p: bm25_weighted(tf, len(tf), avg_len, {t: 1.0 for t in extra}, idfs)
                      for p, tf in tfs.items()} if extra else {}
        c["ranks"]["expanded single"] = G.rank_of(c["target"], order(exp))
        c["ranks"]["expanded fused"] = G.rank_of(c["target"], order(fuse(c.pop("per_bm") + ([only_extra] if extra else []))))
        c.pop("marks", None)
    print(f">>> expansion scored, {(time.time() - t0) / 60:.0f} min; learning the ranker", flush=True)
    weights = cross_validate(cases)

    table = {}
    for c in codes + ["all"]:
        cs = [x for x in cases if c == "all" or x["attribute"] == c]
        table[c] = {m: metrics([x["ranks"].get(m) for x in cs]) for m in METHODS}
    md = ["### Ranking experiment: BM25 and a learned ranker against today's search" + ("" if a.commit else " (dry run, sample)"), "",
          f"{len(answers):,} validated answers" + (f" ({limit} per attribute)" if limit else "") +
          f"; {len(cases):,} usable (cited document in the library, its text in the database, the quote found in it)"
          + (f"; stopped by the {a.budget}-minute budget" if cut else "") + ". Skipped: "
          + (", ".join(f"{k} {v:,}" for k, v in skipped.most_common()) or "none") + ".", "",
          "Share of answers whose cited page is in the top 1 / 3 / 10, and mean reciprocal rank. \"best-of\" picks the "
          "luckiest of the attribute's alternative queries after the fact, so it flatters; the single and learned "
          "rankings are one list, as a reader gets.", "",
          "| Attribute | Answers | " + " | ".join(METHODS) + " |", "| --- | --: |" + " --- |" * len(METHODS)]
    for c in codes + ["all"]:
        n = table[c][METHODS[0]]["n"]
        if not n:
            continue
        name = "All attributes" if c == "all" else f"{c} {cfg['attributes'][c]['name']}"
        cells = [f"{m['top 1']:.0%} / {m['top 3']:.0%} / {m['top 10']:.0%}, {m['mrr']:.2f}" for m in (table[c][k] for k in METHODS)]
        md.append(f"| {name} | {n:,} | " + " | ".join(cells) + " |")
    md += ["", "Words added to each attribute's query (learned from all usable answers' cited pages; each run "
           "scores a company with words learned from the other companies only):", ""]
    md += [f"- {c} {cfg['attributes'][c]['name']}: {', '.join(everyone.get(c, [])) or 'none'}" for c in codes if everyone.get(c)]
    md += ["", "Learned weights (trained on all usable answers; features scaled 0 to 1): "
           + ", ".join(f"{f} {w:+.2f}" for f, w in sorted(zip(FEATURES, weights), key=lambda t: -abs(t[1]))) + "."]
    open("rank_eval.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)

    for m in METHODS:
        allm = table["all"][m]
        if allm["n"]:
            run.evaluation(f"page ranking {m}", allm["n"], allm["top 3"], version="rank_eval.py", suite="retrieval",
                           per_class={c: round(table[c][m]["top 3"], 4) for c in codes if table[c][m]["n"]},
                           notes=f"top 1 {allm['top 1']:.3f}, top 3 {allm['top 3']:.3f}, top 10 {allm['top 10']:.3f}, "
                                 f"MRR {allm['mrr']:.3f}; accuracy is top 3")
    run.finish("ok", rows_in=len(answers), rows_out=len(cases), counters=dict(skipped))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("rank_eval.md", "w").write("### Ranking experiment: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
