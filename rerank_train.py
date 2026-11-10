#!/usr/bin/env python3
"""
rerank_train.py: a page reranker learned from the analysts' validated answers.

The analysts have validated thousands of answers; each one says, through its figure, which page of which
document holds it. That is a training set for the one question the reader loses most on: "does this page
hold this attribute's figure?" This module builds (attribute text, page text, holds or not) pairs from the
validated answers of the development attributes and fine-tunes a small multilingual cross-encoder on them.
The model reads the attribute's name and definition against a page's text, so it can score a page for an
attribute it never saw (a held-out one, or a 2026 one): the generalisation the hand-written labels do not give.

    python rerank_train.py --out rerank_model [--skip N] [--per-attribute 300] [--epochs 1]

Training answers: for each attribute of attributes.json, the validated answers AFTER the first --skip
(default 100), because extract_eval's evaluations read the first ones (--limit); the model is never trained on
an answer it is scored on. Held-out attributes never enter the training set. Positives: the pages printing
the validated figure (found_on, as the oracle finds them). Negatives: pages of the same documents from the
label-matching pool that do not print it (hard negatives), up to NEG_PER_POS each.

The trained model is used by extract_eval --select rerank: the pool is reordered by the cross-encoder's
score before pages_for_model. Read-only on the databases; writes the model directory given with --out.
"""
import argparse, collections, json, os, random, sys, time

BASE_MODEL = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"      # multilingual, 118 MB, reads a query against a passage
PAGE_CHARS = 1500
NEG_PER_POS = 4
SEED = 7


def attribute_text(attr):
    """What the model reads as the query: name and definition, never the labels (the point is to work without them)."""
    return f"{attr.get('name', '')}. {(attr.get('definition') or '')[:300]}"


def page_text(body):
    return (body or "")[:PAGE_CHARS]


def pairs_for_answer(cur, g, attr, spec, X):
    """[(query, page text, label)] for one validated answer: its holding pages (1) and hard negatives (0)."""
    from value_eval import found_on, printed_forms, scales_for, tsquery_for
    docs, weight = X.year_documents(cur, g["wba"], g["year"], attr)
    if not docs:
        return []
    ids = [d[0] for d in docs]
    forms = printed_forms(g["value"], scales_for(attr.get("unit_family"))) if g.get("value") else []
    if not forms:
        return []
    q = " | ".join(f"({tsquery_for(v, d)})" for v, _, d in forms)
    cur.execute("select library_id, page, body from public.pages where library_id = any(%s) and words @@ to_tsquery('simple', %s) limit 40", (ids, q))
    holding = [(lid, page, body) for lid, page, body in cur.fetchall() if any(found_on(body, v) for v, _, _ in forms)]
    if not holding:
        return []
    hold = {(lid, page) for lid, page, _ in holding}
    rows = X.ranked_pages(cur, ids, weight, g["year"], attr, spec)
    negatives = [(lid, page, body) for _, lid, page, body, _ in rows if (lid, page) not in hold]
    random.Random(SEED + hash(g["id"]) % 1000).shuffle(negatives)
    query = attribute_text(attr)
    out = [(query, page_text(body), 1) for _, _, body in holding[:2]]
    out += [(query, page_text(body), 0) for _, _, body in negatives[:NEG_PER_POS * len(out)]]
    return out


def build_pairs(cur, token, spec, bases, skip, per_attribute, X):
    """The training pairs over the development attributes, answers after the first `skip` per attribute."""
    from value_eval import gold_values
    pairs, counts = [], collections.Counter()
    for code, attr in spec["attributes"].items():
        if attr.get("type", "number") != "number":
            continue
        rows = [g for b in bases for g in gold_values(token, b, [code]) if g["value"]][skip:skip + per_attribute]
        for g in rows:
            try:
                got = pairs_for_answer(cur, g, attr, spec, X)
            except Exception as e:                 # one answer's database trouble is not the set's
                print(f"  {g['id']}: {type(e).__name__}", flush=True)
                try:
                    cur.connection.rollback()
                except Exception:
                    pass
                continue
            pairs += got
            counts[code] += bool(got)
    return pairs, counts


def train(pairs, out_dir, epochs=1, batch=16):
    """Fine-tunes the cross-encoder on the pairs and saves it to out_dir. Returns the number of pairs."""
    from sentence_transformers import CrossEncoder, InputExample
    from torch.utils.data import DataLoader
    random.Random(SEED).shuffle(pairs)
    examples = [InputExample(texts=[q, p], label=float(y)) for q, p, y in pairs]
    model = CrossEncoder(BASE_MODEL, num_labels=1, max_length=384)
    loader = DataLoader(examples, shuffle=True, batch_size=batch)
    model.fit(train_dataloader=loader, epochs=epochs, warmup_steps=max(10, len(examples) // (batch * 10)), show_progress_bar=False)
    model.save(out_dir)
    return len(examples)


def ensure_model(cur, token, spec, bases, out_dir, X, skip=100, per_attribute=40, epochs=1):
    """Trains the model into out_dir when it is not there yet (an evaluation run trains then reads); returns the
    directory, or None when too few positives were found. per_attribute 40 since 9 Nov 2026: building the pairs
    ranks every page of every answer, about 14 seconds each, and 80 per attribute took 2.3 hours of the run's
    budget (run 42635969792, which then failed on a missing library)."""
    if os.path.isdir(out_dir) and os.listdir(out_dir):
        return out_dir
    import sentence_transformers, datasets, torch, accelerate  # noqa: F401  fail in the first second, not after the hours of pair building (9 Nov 2026; accelerate: run 42705001779)
    t0 = time.time()
    pairs, counts = build_pairs(cur, token, spec, bases, skip, per_attribute, X)
    pos = sum(1 for _, _, y in pairs if y)
    print(f">>> reranker: {len(pairs):,} pairs ({pos:,} positive) from {sum(counts.values()):,} answers in {time.time() - t0:.0f}s", flush=True)
    if pos < 50:
        return None
    train(pairs, out_dir, epochs)
    print(f">>> reranker trained in {time.time() - t0:.0f}s", flush=True)
    return out_dir


class Reranker:
    """Scores (query, page) pairs with the trained model; used by extract_eval --select rerank."""
    def __init__(self, path):
        from sentence_transformers import CrossEncoder
        self.m = CrossEncoder(path, max_length=384)

    def scores(self, attr, bodies):
        q = attribute_text(attr)
        return [float(s) for s in self.m.predict([(q, page_text(b)) for b in bodies], batch_size=32, show_progress_bar=False)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="rerank_model")
    ap.add_argument("--skip", type=int, default=100, help="answers per attribute left to the evaluations (extract_eval --limit)")
    ap.add_argument("--per-attribute", type=int, default=300)
    ap.add_argument("--epochs", type=int, default=1)
    a = ap.parse_args()
    import evidence_db, extract_eval as X
    token = os.environ["GOLD_TOKEN"].strip()
    spec = json.load(open(X.SPEC, encoding="utf-8"))
    gold_cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "gold_attributes.json"), encoding="utf-8"))
    conn = evidence_db.connect()
    cur = conn.cursor()
    cur.execute("set statement_timeout = '30s'")
    t0 = time.time()
    pairs, counts = build_pairs(cur, token, spec, list(gold_cfg["bases"].values()), a.skip, a.per_attribute, X)
    pos = sum(1 for _, _, y in pairs if y)
    print(f">>> {len(pairs):,} pairs ({pos:,} positive) from {sum(counts.values()):,} answers in {time.time() - t0:.0f}s: "
          + ", ".join(f"{c} {n}" for c, n in counts.items()), flush=True)
    if pos < 50:
        print(">>> too few positives to train; nothing saved")
        return 1
    n = train(pairs, a.out, a.epochs)
    print(f">>> trained on {n:,} pairs in {time.time() - t0:.0f}s; model saved to {a.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
