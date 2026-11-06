#!/usr/bin/env python3
"""
page_select.py: the learned choice of the model reader's rank half (extract_eval --select learned).

The model reader is shown MODEL_PAGES (8) pages: half by the rule candidates, half by rank (pages_for_model).
"learned" reorders the rank half with a logistic regression over page features (today's rank, rule candidate,
document-type prior, query expansion score, meaning, numbers, position), trained on where the analysts'
validated numbers are. On 402 validated answers (select_eval.py, run of 5 Nov 2026) it put the number on the
8 pages for 65% against 63% today, with no attribute worse (Revenue +5, Employees +3 points). It ships as an
option (CLAUDE.md, no going back): base stays the default until the model reader, run with both on the same
answers, shows no attribute more than 3 points worse.

The trained model lives in select_learned.json, written by `select_eval.py --save select_learned.json`. It
holds per attribute the weights, type prior and expansion words trained without each company fold (used when
scoring validated answers, so a company is never scored with what its own cited pages taught) and once on all
folds (used for answers nobody has validated), plus the word weights (idf) of the expansion words. It has
counts and weights only, no company and no answer.

Meaning needs sentence-transformers on the runner (the workflow installs it when select is learned); the
feature is part of what was measured, so a run without it stops instead of scoring something else.
"""
import collections, json, math, os, re

import rank_eval as R

MODEL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "select_learned.json")
EMBED_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
BODY_KEEP = 6000                 # characters of each pool page the features look at (as in select_eval)
NUMBER = re.compile(r"\d[\d.,]{2,}")
FEATURE_NAMES = ["rank", "has a rule candidate", "candidate score", "type prior", "expansion score", "meaning",
                 "numbers", "position"]


def expansion_scores(pool, terms, idfs, avg_len):
    """BM25 of each pool page over the expansion words (EXPAND_WEIGHT each). pool: [(s, lid, page, body, top)]."""
    if not terms:
        return [0.0] * len(pool)
    w = {t: R.EXPAND_WEIGHT for t in terms}
    out = []
    for _, _, _, body, _ in pool:
        tf = collections.Counter(R.tokens(body))
        out.append(R.bm25_weighted(tf, len(tf), avg_len, w, idfs))
    return out


def features(pool, types, i, lift, exp, emb):
    """The FEATURE_NAMES of pool page i. types: {library_id: doc_type}; lift: {doc_type: prior}; exp: the
    expansion scores; emb: the meaning similarities or None."""
    s, lid, page, body, top = pool[i]
    lifts = math.log(lift.get(types.get(lid, ""), 1.0))
    nums = len(NUMBER.findall(body[:BODY_KEEP]))
    mx = max(exp) or 1.0
    return [1.0 / (1 + i), 1.0 if top else 0.0, max(min((top[0] if top else 0) / 10.0, 1.0), -1.0), lifts,
            exp[i] / mx, emb[i] if emb else 0.0, min(nums / 60.0, 1.0), min(page / 300.0, 1.0)]


def query_text(attr):
    return f"{attr.get('name')}: {'; '.join(attr.get('queries', [])[:3])}"


class Embedder:
    def __init__(self):
        from sentence_transformers import SentenceTransformer
        self.m = SentenceTransformer(EMBED_MODEL)

    def sims(self, query, texts):
        q = self.m.encode([query], normalize_embeddings=True)
        t = self.m.encode([x[:1500] for x in texts], normalize_embeddings=True, batch_size=32)
        return list((t @ q.T).ravel().astype(float))


def load(path=MODEL_FILE):
    """The trained model, or None when the file is missing."""
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def part_for(model, code, fold=None):
    """The trained part for an attribute: the one trained without `fold` (a validated answer's company fold),
    or the one trained on all folds (fold None). None when the attribute was not trained."""
    m = ((model or {}).get("attributes") or {}).get(code)
    if not m:
        return None
    p = m.get(str(fold)) if fold is not None else m.get("all")
    return p if p and p.get("w") else None


def order(model, code, attr, pool, types, fold=None, embedder=None):
    """Indices of pool, best first by the learned score, or None when there is nothing trained for the
    attribute (the caller then keeps today's order). pool: [(score, library_id, page, body, top)] in rank
    order, as keep_pages returns it."""
    part = part_for(model, code, fold)
    if not part or not pool:
        return None
    short = [(s, lid, page, (body or "")[:BODY_KEEP], top) for s, lid, page, body, top in pool]
    exp = expansion_scores(short, part.get("terms") or [], model.get("idf") or {}, model.get("avg_len") or 1)
    sims = embedder.sims(query_text(attr), [p[3] for p in short]) if embedder else None
    sc = [R.score(part["w"], features(short, types, i, part.get("lift") or {}, exp, sims)) for i in range(len(short))]
    return sorted(range(len(short)), key=lambda i: -sc[i])
