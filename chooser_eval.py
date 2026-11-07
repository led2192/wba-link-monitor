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


UP = re.compile(r"\b(?:increas\w*|grew|grow\w*|rose|rise|rising|up\b|higher|improv\w*|gain\w*)")
DOWN = re.compile(r"\b(?:decreas\w*|declin\w*|fell|fall\w*|drop\w*|down\b|lower|reduc\w*)")


def memory_features(v, row_vals, known, year, near_text):
    """Signals from the company's validated figures for other years (the owner's idea, 7 Nov 2026): a figure
    of the right size for this company, a row that also prints a known year's figure (a table of years), and
    a figure whose change from last year agrees with the words around it ("increased", "fell")."""
    f = {}
    if not known or v <= 0:
        return f
    f["memory: known"] = 1.0
    y0 = min(known, key=lambda y: abs(y - year))
    r = abs(math.log10(v / known[y0])) if known[y0] > 0 else 9
    f["memory: same size (within 25%)"] = 1.0 if r < 0.1 else 0.0
    f["memory: within 2 times"] = 1.0 if 0.1 <= r < 0.3 else 0.0
    f["memory: within 10 times"] = 1.0 if 0.3 <= r < 1 else 0.0
    f["memory: 10 times off or more"] = 1.0 if r >= 1 else 0.0
    if any(any(E.right(x, k) for k in known.values()) for x in row_vals if x != v):
        f["memory: the row prints a known year's figure"] = 1.0
    if any(E.right(v, k) for k in known.values()):
        f["memory: equals a known year's figure"] = 1.0
    prev = known.get(year - 1)
    if prev and prev > 0:
        up, down = bool(UP.search(near_text)), bool(DOWN.search(near_text))
        if up != down:
            agrees = (v > prev) == up
            f["memory: change agrees with the words"] = 1.0 if agrees else 0.0
            f["memory: change contradicts the words"] = 0.0 if agrees else 1.0
    return f


# ----- "fancy hits": where and how a word is printed (7 Nov 2026, after Google's hit lists) -----
# pypdf text, which the evidence database holds, has no type sizes or weights. PyMuPDF reads them from the
# PDF (about 0.3 s a page): a span in type at least HEADING times the page's body size is a heading, a span
# with the bold flag is bold, a span in the top TOP of the page is near the top.
HEADING, TOP, BOLD = 1.2, 0.15, 16
DIGITS = re.compile(r"\d[\d,.' \u00a0\u202f]*\d|\d")


def page_style(path, page):
    """{"heading", "bold", "top": lower-case text; "bold_figures", "large_figures": digit strings} of one page
    of a PDF, or None."""
    try:
        import pymupdf
        doc = pymupdf.open(path)
        p = doc[int(page) - 1]
        h = p.rect.height or 1
        spans = [(s["text"], s["size"], s["flags"], s["bbox"][1] / h) for b in p.get_text("dict")["blocks"]
                 for l in b.get("lines", []) for s in l.get("spans", []) if s.get("text", "").strip()]
        doc.close()
    except Exception:
        return None
    if not spans:
        return None
    sizes = sorted((sz, len(t)) for t, sz, _, _ in spans)
    half, acc, body = sum(n for _, n in sizes) / 2, 0, sizes[0][0]
    for sz, n in sizes:                       # the size of the median character: the body text
        acc += n
        if acc >= half:
            body = sz; break
    big = lambda sz: sz >= HEADING * body
    digits = lambda t: {re.sub(r"\D", "", m) for m in DIGITS.findall(t) if len(re.sub(r"\D", "", m)) >= 2}
    return {"heading": " ".join(t.lower() for t, sz, _, _ in spans if big(sz)),
            "bold": " ".join(t.lower() for t, _, fl, _ in spans if fl & BOLD),
            "top": " ".join(t.lower() for t, _, _, y in spans if y < TOP),
            "bold_figures": set().union(*[digits(t) for t, _, fl, _ in spans if fl & BOLD]),
            "large_figures": set().union(*[digits(t) for t, sz, _, _ in spans if big(sz)])}


def fancy_features(style, label, token):
    if not style:
        return {}
    lab = label.lower()
    d = re.sub(r"\D", "", token)
    return {"fancy: page read": 1.0,
            "fancy: label in a heading": 1.0 if lab in style["heading"] else 0.0,
            "fancy: label in bold": 1.0 if lab in style["bold"] else 0.0,
            "fancy: label near the top": 1.0 if lab in style["top"] else 0.0,
            "fancy: figure in bold": 1.0 if d and d in style["bold_figures"] else 0.0,
            "fancy: figure in large type": 1.0 if d and d in style["large_figures"] else 0.0}


def all_candidates(text, year, attr, spec, page_rank, doc_type, doc_offset, known=None, style=None):
    """[(features: {name: value}, value, rule_score)] for every figure of every labelled row of a page.
    rule_score is extract_eval.candidates's hand score for the column it would take (else -inf). known:
    {year: value} of the company's validated figures for other years of this attribute."""
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
            f.update(memory_features(v * scale, [x * scale for x in vals], known, year, low[max(0, pos - 150):pos + 150]))
            f.update(fancy_features(style, lab, tok))
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


def shuffled(cases, seed=7):
    """The control: each training answer's right flag moved to a random candidate of the same answer. A model
    fitted on it must score near chance on the real answers; if it does not, a feature carries the answer
    itself (a leak) and the measure is wrong."""
    import random
    rnd = random.Random(seed)
    out = []
    for c in cases:
        if not c["cands"]:
            continue
        k = rnd.randrange(len(c["cands"]))
        out.append(dict(c, cands=[(f, v, r, i == k) for i, (f, v, r, _p) in enumerate(c["cands"])]))
    return out


def fit(cases, l2=0.01, steps=300, lr=0.5, keep=None):
    """Conditional logit weights fitted on cases with at least one right candidate (Adam, L2). keep: a test
    on feature names (None = all)."""
    cases = [c for c in cases if any(p for *_, p in c["cands"])]
    support = collections.Counter(k for c in cases for k in {k for f, *_ in c["cands"] for k in f} if keep is None or keep(k))
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
    best, bi = (-math.inf, False), None
    for i, (f, _v, _r, pos) in enumerate(case["cands"]):
        s = (sum(w[j] * v for j, v in vocab.ids(f, False)), not pos)     # on a tie, the wrong candidate wins
        if s > best:
            best, bi = s, i
    return bi


def value_probs(w, vocab, case):
    """{value key: (probability, right)}: the softmax over the case's candidates, summed over candidates that
    print the same value (the same figure on several pages is one answer)."""
    if not case["cands"]:
        return {}
    sc = [sum(w[j] * v for j, v in vocab.ids(f, False)) for f, *_ in case["cands"]]
    m = max(sc)
    e = [math.exp(x - m) for x in sc]
    z = sum(e)
    out = {}
    for (f, val, rule, pos), p in zip(case["cands"], e):
        k = vkey(val)
        q, r = out.get(k, (0.0, False))
        out[k] = (q + p / z, r or bool(pos))
    return out


def vkey(v):
    """A value rounded to 4 significant digits: the key that merges the same figure printed twice."""
    return float(f"{v:.4g}") if v else 0.0


# ----- conformal prediction (7 Nov 2026) -----
# Split conformal prediction over the learned probabilities: the nonconformity of an answer is 1 - p(the
# analyst's value). With q the ceil((n+1)(1-alpha))/n quantile of the scores of the other folds, the set
# {values: 1 - p <= q} holds the analyst's value with probability at least 1 - alpha, for answers whose value
# is among the candidates (finite-sample, distribution-free). A set of one value can be written without a
# person; a larger set is a question for a person or a targeted model call. Selective acceptance: the
# smallest probability threshold whose accepted answers in the other folds are right at least 1 - alpha.

def top_of(vp):
    """(probability, right) of the most probable value; on a tie the wrong one is taken, so a tie never flatters
    the measure."""
    return max(vp.values(), key=lambda x: (x[0], not x[1]))


def conformal_q(scores, alpha):
    if not scores:
        return None
    n = len(scores)
    k = min(n, math.ceil((n + 1) * (1 - alpha)))
    return sorted(scores)[k - 1]


def selective_threshold(pairs, alpha, min_n=20):
    """pairs: [(top probability, right)]. The lowest threshold t whose answers with p >= t are right at least
    1 - alpha of the time (at least min_n of them), or None."""
    best = None
    for t in sorted({round(p, 3) for p, _ in pairs}, reverse=True):
        acc = [r for p, r in pairs if p >= t]
        if len(acc) >= min_n and sum(acc) / len(acc) >= 1 - alpha:
            best = t
    return best


def conformal_report(code, cs, alphas=(0.02, 0.05, 0.10)):
    """Report lines for one attribute from the out-of-fold value probabilities in c["vp"]."""
    out = []
    for alpha in alphas:
        covered = singles = singles_right = sizes = n = 0
        acc_n = acc_right = 0
        for f in range(R.FOLDS):
            calib = [c for c in cs if c["fold"] != f and c.get("vp")]
            test = [c for c in cs if c["fold"] == f and c.get("vp") is not None]
            scores = [1 - max((p for p, r in c["vp"].values() if r), default=0.0) for c in calib if any(r for _, r in c["vp"].values())]
            q = conformal_q(scores, alpha)
            t = selective_threshold([top_of(c["vp"]) for c in calib if c["vp"]], alpha)
            for c in test:
                n += 1
                if not c["vp"] or q is None:
                    continue
                cset = [(p, r) for p, r in c["vp"].values() if 1 - p <= q]
                sizes += len(cset)
                covered += any(r for _, r in cset)
                if len(cset) == 1:
                    singles += 1; singles_right += cset[0][1]
                top_p, top_r = top_of(c["vp"])
                if t is not None and top_p >= t:
                    acc_n += 1; acc_right += top_r
        if n:
            out.append(f"| {code} | {alpha:.0%} | {covered / n:.0%} | {sizes / n:.1f} | {singles / n:.0%} | "
                       f"{(singles_right / singles) if singles else 0:.0%} | {acc_n / n:.0%} | {(acc_right / acc_n) if acc_n else 0:.0%} |")
    return out


# ----- joint choice of Scope 2 location and market (7 Nov 2026) -----
PAIR = ("GMC_00002.1", "GMC_00003.1")


def joint_choice(vl, vm):
    """(location value, market value) maximising log p_l + log p_m with two different values; each a key of the
    value_probs dicts. None when either side has no candidate."""
    if not vl or not vm:
        return None
    best, pick = -math.inf, None
    for a, (pa, _) in vl.items():
        for b, (pb, _) in vm.items():
            if a == b:
                continue
            s = math.log(pa + 1e-12) + math.log(pb + 1e-12)
            if s > best:
                best, pick = s, (a, b)
    return pick


def gather(cur, g, attr, spec, known=None, fancy=False):
    """The candidates of one answer on its kept pages, each with whether it equals the analyst's value. With
    fancy, each page is also read from its PDF for type sizes and weights (the PDFs are deleted after the
    answer: the runner's disk would not hold them all)."""
    docs, weight = E.year_documents(cur, g["wba"], g["year"], attr)
    if not docs:
        return None
    want = E.document_year(g["year"], E.fy_end_month(cur, g["wba"]))
    meta = {d[0]: (d[1], d[2]) for d in docs}
    rows = E.ranked_pages(cur, [d[0] for d in docs], weight, g["year"], attr, spec, {}, None, None)
    pages = E.keep_pages(rows)
    cands, paths = [], {}
    for rank, (_, lid, page, body, _top) in enumerate(pages):
        dt, dy = meta.get(lid, ("", want))
        style = None
        if fancy:
            import table_text
            if lid not in paths:
                paths[lid] = table_text.pdf_path(lid)
            style = page_style(paths[lid], page) if paths[lid] else None
            FANCY_STATS["pages"] += 1; FANCY_STATS["read"] += bool(style)
        for f, v, rule in all_candidates(body, g["year"], attr, spec, rank, dt, (dy or want) - want, known, style):
            cands.append((f, v, rule, E.right(v, g["value"])))
    for p in paths.values():
        if p:
            try:
                os.remove(p)
            except OSError:
                pass
    return cands


FANCY_STATS = collections.Counter()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=150)
    ap.add_argument("--attributes", default="")
    ap.add_argument("--budget", type=int, default=200)
    ap.add_argument("--l2", type=float, default=0.01)
    ap.add_argument("--fancy", action="store_true", help="also read each kept page from its PDF for type sizes and weights (slow: a download per document)")
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
    gold_index = {(g["wba"], g["attribute"], g["year"]): g["value"] for g in answers}
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
                known = {y: v for (w, c, y), v in gold_index.items() if w == g["wba"] and c == g["attribute"] and y != g["year"]}
                cands = gather(cur, g, spec["attributes"][g["attribute"]], spec, known, a.fancy)
                fails = 0
            except Exception as e:
                conn.rollback(); cur.execute("set statement_timeout = '30s'")
                fails += 1
                if fails >= E.DB_FAILS_IN_A_ROW:
                    print(f">>> stopped: the database failed {fails} times in a row ({type(e).__name__})", flush=True)
                    break
                continue
            cases[g["attribute"]].append({"wba": g["wba"], "year": g["year"], "fold": R.fold_of(g["wba"]), "cands": cands or []})

    md = ["### Choosing the figure with a probability model learned from the analysts' answers", "",
          f"{sum(len(v) for v in cases.values()):,} validated answers" + ("; stopped at the time budget" if cut else "")
          + ". Candidates: every figure printed after one of the attribute's labels on the 25 kept pages (up to six columns a row). "
            "Rules: the hand-weighted choice of extract_eval.candidates on the same candidates. Learned: a conditional logit fitted on "
            "the other four company folds. With the company's other years: the same plus the size of the figure against the company's "
            "validated figures for other years, a row that prints one of them, and a change from last year that agrees or not with the "
            "words around it. Ceiling: a candidate equals the analyst's value. Control: the same model trained on answers whose right "
            "candidate was moved at random; it must stay near chance, or a feature leaks the answer. Ties count as wrong. "
            "No language model, so a rerun gives the same numbers. Counts and shares only.", "",
          "| Attribute | Answers | With candidates | Ceiling | Rules | Learned | + the company's other years | + type size and weight | Answers with another validated year | Candidates per answer | Control: trained on shuffled answers | Chance |",
          "| --- | --: | --: | --: | --: | --: | --: | --: | --: | --: | --: | --: |"]
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
        learned_right, memory_right, fancy_right, control_right, chance = 0, 0, 0, 0, 0.0
        with_memory = sum(1 for c in cs if any("memory: known" in f for f, *_ in c["cands"]))
        no_memory = lambda k: not k.startswith(("memory:", "fancy:"))
        no_fancy = lambda k: not k.startswith("fancy:")
        for f in range(R.FOLDS):
            train = [c for c in cs if c["fold"] != f]
            test = [c for c in cs if c["fold"] == f]
            if not test or not train:
                continue
            w, vocab = fit(train, a.l2, a.steps, keep=no_memory)
            wm, vm = fit(train, a.l2, a.steps, keep=no_fancy)
            wc, vc = fit(shuffled(train), a.l2, a.steps, keep=no_fancy)
            wf, vf = fit(train, a.l2, a.steps) if a.fancy else (None, None)
            for c in test:
                i = choose(w, vocab, c)
                learned_right += bool(i is not None and c["cands"][i][3])
                i = choose(wm, vm, c)
                memory_right += bool(i is not None and c["cands"][i][3])
                i = choose(wc, vc, c)
                control_right += bool(i is not None and c["cands"][i][3])
                chance += (sum(1 for *_, p in c["cands"] if p) / len(c["cands"])) if c["cands"] else 0.0
                if a.fancy:
                    i = choose(wf, vf, c)
                    fancy_right += bool(i is not None and c["cands"][i][3])
                c["vp"] = value_probs(wf, vf, c) if a.fancy else value_probs(wm, vm, c)   # the fullest model, out of fold
        per = sum(len(c["cands"]) for c in cs) / max(withc, 1)
        name = spec["attributes"][code].get("name", "")
        md.append(f"| {code} {name} | {n:,} | {withc / n:.0%} | {ceil / n:.0%} | {rules_right / n:.0%} | **{learned_right / n:.0%}** | **{memory_right / n:.0%}** | {f"**{fancy_right / n:.0%}**" if a.fancy else "-"} | {with_memory / n:.0%} | {per:.0f} | {control_right / n:.0%} | {chance / n:.0%} |")
        w, vocab = fit(cs, a.l2, a.steps)
        inv = {i: k for k, i in vocab.ix.items()}
        top = sorted(range(len(w)), key=lambda i: -w[i])[:8]
        bottom = sorted(range(len(w)), key=lambda i: w[i])[:8]
        weights_md.append(f"- {code}: up " + ", ".join(f"{inv[i]} ({w[i]:+.1f})" for i in top)
                          + "; down " + ", ".join(f"{inv[i]} ({w[i]:+.1f})" for i in bottom) + ".")
    conf = [l for code in codes for l in conformal_report(code, cases.get(code, []))]
    if conf:
        md += ["", "#### How many answers could be written without a person (conformal prediction on the fullest learned model)", "",
               "Out-of-fold probabilities, the same figure on several pages merged. Set: the values whose nonconformity (1 - p) is within the "
               "conformal quantile of the other folds, so the set holds the analyst's value at least 1 - alpha of the time when the value is "
               "among the candidates. Coverage is over all answers (an answer whose value is not among the candidates is never covered). "
               "Accepted: answers whose top probability passes the threshold that kept the other folds' accepted answers right 1 - alpha of the time.", "",
               "| Attribute | alpha | Coverage | Set size | One value | One value right | Accepted | Accepted right |",
               "| --- | --: | --: | --: | --: | --: | --: | --: |"] + conf
    loc, mkt = ({(c["wba"], c["year"]): c for c in cases.get(k, []) if c.get("vp") is not None} for k in PAIR)
    both = [k for k in loc if k in mkt]
    if both:
        ind = jnt = 0
        for k in both:
            vl, vm = loc[k]["vp"], mkt[k]["vp"]
            il = max(vl.items(), key=lambda x: (x[1][0], not x[1][1]))[0] if vl else None
            im = max(vm.items(), key=lambda x: (x[1][0], not x[1][1]))[0] if vm else None
            ind += bool(il is not None and vl[il][1]) + bool(im is not None and vm[im][1])
            pick = joint_choice(vl, vm)
            if pick:
                jnt += bool(vl[pick[0]][1]) + bool(vm[pick[1]][1])
            else:
                jnt += bool(il is not None and vl[il][1]) + bool(im is not None and vm[im][1])
        md += ["", f"Scope 2 location and market chosen together ({len(both)} company years with both answers): right "
               f"{ind / (2 * len(both)):.0%} chosen apart, {jnt / (2 * len(both)):.0%} chosen together (the most probable pair of two "
               "different values)."]
    if a.fancy:
        md += ["", f"Type size and weight: {FANCY_STATS['read']:,} of {FANCY_STATS['pages']:,} kept pages read from their PDF "
               "(heading: type 1.2 times the page's body size or more; bold: the font's bold flag; top: the top 15% of the page)."]
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
