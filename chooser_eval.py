#!/usr/bin/env python3
"""
chooser_eval.py: choose the figure with a probability model learned from the analysts' answers.

The readers lose most answers not because the right figure is missing from the pages they read but because they
choose another figure printed near it: another year's column, the other Scope 2 method, a subtotal, a cost line
(7 Nov 2026: the validated figure was on the model's pages for 66% to 72% of answers, and right for 58% to
63%). The rule reader chooses with hand-set weights (extract_eval.candidates: +2 for a thousands separator,
+1 for a year header...). This script learns those weights from the validated answers instead, with no model:

  1. candidates: on the kept pages of the answer (extract_eval's ranking, the same 25 pages the rule reader
     reads), every figure printed after one of the attribute's labels, up to six columns of each row: its
     label, its column and whether that column's header is the year asked or the next, the words of the row,
     the size of the row, separators and digits, prose around it, the page's stated scale, the page's rank,
     the document's type and year;
  2. a conditional logit (a softmax over the candidates of one answer, the model of discrete choice): the
     probability that the analyst chose candidate c is exp(w.x_c) / sum over the answer's candidates of
     exp(w.x). The weights w are fitted by gradient descent with an L2 penalty, per attribute, on the answers
     of four company folds and scored on the fifth (rank_eval.fold_of), so a company is never scored with
     what its own answers taught;
  3. scored against the rule reader's own choice on the same candidates (the hand weights) and against the
     ceiling (a candidate equals the analyst's value).

Counts, shares and the strongest weights (single words, columns, document types) only in the report (it is
public). Read-only: gold bases (GOLD_TOKEN), evidence database (SUPABASE_DB_URL). Options: --limit N answers
per attribute (150), --attributes, --budget minutes (200), --l2, --steps.
"""
import argparse, collections, json, math, os, re, sys, time, traceback

import numpy as np

import qa
import extract_eval as E
import rank_eval as R

MAX_COLS = 6
NEAR = 60                      # characters of the row read for its words
WORD = re.compile(r"[a-zA-ZÀ-ɏ][a-zA-ZÀ-ɏ\-]{2,}")
STOP = set("the and for from with that this are was were has have its our their which into total".split())


def row_words(text, pos):
    """The words of the row a label starts: from the line start to NEAR characters after the label."""
    start = text.rfind("\n", 0, pos) + 1
    seg = text[start:pos + NEAR].lower()
    return {w for w in WORD.findall(seg) if w not in STOP}


def all_candidates(text, year, attr, spec, page_rank, doc_type, doc_offset):
    """[(features: {name: value}, value, rule_score)] for every figure of every labelled row of a page.
    rule_score is extract_eval.candidates's hand score for the column it would take (else -inf)."""
    family = attr.get("unit_family", "currency")
    labels = attr.get("labels", [])
    col_year = E.year_header(text, year)
    col_next = E.year_header(text, year + 1)
    scale = E.page_scale(text, family, spec)
    low = text.lower()
    rules = {}
    for sc, v, raw, s, lab in E.candidates(text, year, attr, spec):
        rules.setdefault((round(v, 6), lab), sc)
    out = []
    for pos, lab in E.label_positions(text, labels, attr.get("exclude", [])):
        nums = E.numbers_after(text, pos)
        if not nums:
            continue
        words = row_words(text, pos)
        narrative = bool(E.NARRATIVE.search(low[pos:pos + 80]))
        vals = [v for v, _, _ in nums[:MAX_COLS]]
        for j, (v, tok, off) in enumerate(nums[:MAX_COLS]):
            digits = len(re.sub(r"\D", "", re.split(r"[.,](?=\d{1,2}$)", tok)[0]))
            f = {"bias": 1.0, f"label={lab.lower()}": 1.0, "label rank": labels.index(lab) / max(len(labels), 1),
                 f"column={j}": 1.0, "row figures": min(len(nums), 6) / 6,
                 "column is the year": 1.0 if col_year == j else 0.0,
                 "column is the next year": 1.0 if col_next == j and col_year != j else 0.0,
                 "page has a year header": 1.0 if col_year is not None else 0.0,
                 "separator": 1.0 if re.search(r"\d[,.'   ]\d{3}", tok) else 0.0,
                 "digits": min(digits, 12) / 12, "small integer": 1.0 if v < 100 and digits <= 2 else 0.0,
                 "right after the label": 1.0 if off <= 20 else 0.0, "prose": 1.0 if narrative else 0.0,
                 "largest of the row": 1.0 if v == max(vals) else 0.0, "smallest of the row": 1.0 if v == min(vals) else 0.0,
                 f"scale={int(scale)}": 1.0, f"page rank={min(page_rank // 5, 4)}": 1.0,
                 f"document={doc_type or 'unknown'}": 1.0, f"document year={doc_offset:+d}": 1.0}
            for w in words:
                f["word=" + w] = 1.0
            out.append((f, v * scale, rules.get((round(v * scale, 6), lab), -math.inf)))
    return out


class Vocab:
    def __init__(self):
        self.ix = {}

    def ids(self, f, grow):
        out = []
        for k, v in f.items():
            if k not in self.ix:
                if not grow:
                    continue
                self.ix[k] = len(self.ix)
            out.append((self.ix[k], v))
        return out


def pack(cases, vocab, grow):
    """Flatten the cases into arrays: per candidate its case, its features and values, and its label."""
    cand_case, feat, val, owner, y, starts = [], [], [], [], [], [0]
    n = 0
    for k, c in enumerate(cases):
        for f, _, _, pos in c["cands"]:
            for i, v in vocab.ids(f, grow):
                feat.append(i); val.append(v); owner.append(n)
            y.append(1.0 if pos else 0.0); cand_case.append(k); n += 1
        starts.append(n)
    return (np.array(cand_case), np.array(feat, dtype=np.int64), np.array(val), np.array(owner, dtype=np.int64),
            np.array(y), np.array(starts))


def scores_of(w, feat, val, owner, n):
    return np.bincount(owner, weights=w[feat] * val, minlength=n)


def softmax_by_case(s, cand_case, k):
    m = np.full(k, -np.inf); np.maximum.at(m, cand_case, s)
    e = np.exp(s - m[cand_case])
    z = np.bincount(cand_case, weights=e, minlength=k)
    return e / z[cand_case]


MIN_SUPPORT = 5                # a feature seen in fewer answers is left out (no company-specific words)


def fit(cases, l2=0.01, steps=300, lr=0.5):
    """Conditional logit weights fitted on cases with at least one right candidate (Adam, L2)."""
    cases = [c for c in cases if any(p for *_, p in c["cands"])]
    support = collections.Counter(k for c in cases for k in {k for f, *_ in c["cands"] for k in f})
    vocab = Vocab()
    vocab.ix = {k: i for i, k in enumerate(sorted(k for k, n in support.items() if n >= MIN_SUPPORT))}
    cand_case, feat, val, owner, y, starts = pack(cases, vocab, False)
    k, n = len(cases), len(y)
    w = np.zeros(len(vocab.ix)); m1 = np.zeros_like(w); m2 = np.zeros_like(w)
    ypos = y / np.maximum(np.bincount(cand_case, weights=y, minlength=k)[cand_case], 1)   # several right candidates share the mass
    for t in range(1, steps + 1):
        p = softmax_by_case(scores_of(w, feat, val, owner, n), cand_case, k)
        g = np.bincount(feat, weights=(p - ypos)[owner] * val, minlength=len(w)) / max(k, 1) + l2 * w
        m1 = 0.9 * m1 + 0.1 * g; m2 = 0.999 * m2 + 0.001 * g * g
        w -= lr * 0.1 * (m1 / (1 - 0.9 ** t)) / (np.sqrt(m2 / (1 - 0.999 ** t)) + 1e-8)
    return w, vocab


def choose(w, vocab, case):
    """Index of the most probable candidate of a case, or None."""
    if not case["cands"]:
        return None
    best, bi = -math.inf, None
    for i, (f, *_rest) in enumerate(case["cands"]):
        s = sum(w[j] * v for j, v in vocab.ids(f, False))
        if s > best:
            best, bi = s, i
    return bi


def gather(cur, g, attr, spec):
    """The candidates of one answer on its kept pages, each with whether it equals the analyst's value."""
    docs, weight = E.year_documents(cur, g["wba"], g["year"], attr)
    if not docs:
        return None
    want = E.document_year(g["year"], E.fy_end_month(cur, g["wba"]))
    meta = {d[0]: (d[1], d[2]) for d in docs}
    rows = E.ranked_pages(cur, [d[0] for d in docs], weight, g["year"], attr, spec, {}, None, None)
    pages = E.keep_pages(rows)
    cands = []
    for rank, (_, lid, page, body, _top) in enumerate(pages):
        dt, dy = meta.get(lid, ("", want))
        for f, v, rule in all_candidates(body, g["year"], attr, spec, rank, dt, (dy or want) - want):
            cands.append((f, v, rule, E.right(v, g["value"])))
    return cands


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=150)
    ap.add_argument("--attributes", default="")
    ap.add_argument("--budget", type=int, default=200)
    ap.add_argument("--l2", type=float, default=0.01)
    ap.add_argument("--steps", type=int, default=300)
    a = ap.parse_args()
    token = os.environ.get("GOLD_TOKEN", "").strip()
    if not token:
        open("chooser_eval.md", "w").write("### Chooser: GOLD_TOKEN is not set; nothing run.\n")
        return 0
    from value_eval import gold_values
    spec = json.load(open(E.SPEC, encoding="utf-8"))
    gold_cfg = json.load(open(os.path.join(os.path.dirname(E.SPEC), "gold_attributes.json"), encoding="utf-8"))
    codes = [c for c, v in spec["attributes"].items() if v["type"] == "number" and (not a.attributes or c in a.attributes.split(","))]
    run = qa.Run("chooser_eval.py")
    answers = [g for base in gold_cfg["bases"].values() for g in gold_values(token, base, codes)
               if g["value"] and not g["unit"].lower().startswith(("%", "percent"))]
    by = collections.defaultdict(list)
    for g in answers:
        by[g["attribute"]].append(g)
    queues = [by[c][:a.limit] if a.limit else by[c] for c in codes]
    todo = [q[i] for i in range(max((len(q) for q in queues), default=0)) for q in queues if i < len(q)]
    print(f">>> {len(todo):,} answers", flush=True)
    cases = collections.defaultdict(list)
    t0, cut, fails = time.time(), False, 0
    with __import__("evidence_db").connect() as conn:
        cur = conn.cursor()
        cur.execute("set statement_timeout = '30s'")
        for i, g in enumerate(todo, 1):
            if time.time() - t0 > a.budget * 60:
                cut = True; break
            if i % 100 == 0:
                print(f"  {i:,}/{len(todo):,} in {(time.time() - t0) / 60:.0f} min", flush=True)
            try:
                cands = gather(cur, g, spec["attributes"][g["attribute"]], spec)
                fails = 0
            except Exception as e:
                conn.rollback(); cur.execute("set statement_timeout = '30s'")
                fails += 1
                if fails >= E.DB_FAILS_IN_A_ROW:
                    print(f">>> stopped: the database failed {fails} times in a row ({type(e).__name__})", flush=True)
                    break
                continue
            cases[g["attribute"]].append({"wba": g["wba"], "fold": R.fold_of(g["wba"]), "cands": cands or []})

    md = ["### Choosing the figure with a probability model learned from the analysts' answers", "",
          f"{sum(len(v) for v in cases.values()):,} validated answers" + ("; stopped at the time budget" if cut else "")
          + ". Candidates: every figure printed after one of the attribute's labels on the 25 kept pages (up to six columns a row). "
            "Rules: the hand-weighted choice of extract_eval.candidates on the same candidates. Learned: a conditional logit fitted on "
            "the other four company folds. Ceiling: a candidate equals the analyst's value. No model; counts and shares only.", "",
          "| Attribute | Answers | With candidates | Ceiling | Rules | Learned | Candidates per answer |",
          "| --- | --: | --: | --: | --: | --: | --: |"]
    weights_md = []
    for code in codes:
        cs = cases.get(code, [])
        if not cs:
            continue
        n = len(cs); withc = sum(1 for c in cs if c["cands"])
        ceil = sum(1 for c in cs if any(p for *_, p in c["cands"]))
        rules_right = 0
        for c in cs:
            pick = max(range(len(c["cands"])), key=lambda i: c["cands"][i][2], default=None) if c["cands"] else None
            rules_right += bool(pick is not None and c["cands"][pick][2] > -math.inf and c["cands"][pick][3])
        learned_right = 0
        for f in range(R.FOLDS):
            train = [c for c in cs if c["fold"] != f]
            test = [c for c in cs if c["fold"] == f]
            if not test or not train:
                continue
            w, vocab = fit(train, a.l2, a.steps)
            for c in test:
                i = choose(w, vocab, c)
                learned_right += bool(i is not None and c["cands"][i][3])
        per = sum(len(c["cands"]) for c in cs) / max(withc, 1)
        name = spec["attributes"][code].get("name", "")
        md.append(f"| {code} {name} | {n:,} | {withc / n:.0%} | {ceil / n:.0%} | {rules_right / n:.0%} | **{learned_right / n:.0%}** | {per:.0f} |")
        w, vocab = fit(cs, a.l2, a.steps)
        inv = {i: k for k, i in vocab.ix.items()}
        top = sorted(range(len(w)), key=lambda i: -w[i])[:8]
        bottom = sorted(range(len(w)), key=lambda i: w[i])[:8]
        weights_md.append(f"- {code}: up " + ", ".join(f"{inv[i]} ({w[i]:+.1f})" for i in top)
                          + "; down " + ", ".join(f"{inv[i]} ({w[i]:+.1f})" for i in bottom) + ".")
    md += ["", "Strongest weights of the model fitted on all answers (a weight is a log-odds: +0.7 doubles the odds):", ""] + weights_md
    open("chooser_eval.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    run.finish("ok", rows_in=len(todo), rows_out=sum(len(v) for v in cases.values()))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("chooser_eval.md", "w").write("### Chooser: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
