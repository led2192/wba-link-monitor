#!/usr/bin/env python3
"""
evidence_pack.py: the evidence pack of a narrative answer, for narrative_eval --evidence pack3.

A pack is PACK (15) passages of about 120 words, at most 2 from one page, chosen without a model from the
year's documents (the document year and the next): the pages that hold every word of one wording plus the
60 best BM25 pages, cut into sentence passages, scored by BM25 over the attribute's query words and the
expansion words learned from where the analysts' evidence sits (pack_eval.py). On 351 validated ACT Core
Climate answers (6 Nov 2026) the model reading the pack with one to three quotes of up to 60 words was right
for 72% against 70% with today's pages (worst attribute 2.6 points down), found 83% of the yes answers
against 70%, and its quotes held the analyst's evidence for 61% against 50%.

The learned words live in pack_terms.json, written by `pack_eval.py --save pack_terms.json`: per attribute one
part per company fold (trained without that fold, used for validated answers so a company is never served
words its own evidence taught) and one on all folds (for answers nobody has validated), plus the idf of every
word. Words and weights only, no company and no answer. An attribute that is not in the file gets its query
words alone.
"""
import json, os

import rank_eval as R

TERMS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pack_terms.json")


def load(path=TERMS_FILE):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def terms_for(model, code, fold=None):
    """The expansion words for an attribute: trained without `fold`, or on all folds when fold is None."""
    parts = ((model or {}).get("attributes") or {}).get(code) or {}
    p = parts.get("all" if fold is None else str(fold)) or {}
    return list(p.get("terms") or [])


def build(cur, wba, year, fy, code, queries, model, fold=None):
    """(passages, chosen, docs): every candidate passage [(library_id, page, n, text)], the indices of the pack
    in score order, and {library_id: (doc_type, year)} of the year's documents. Empty when the company has no
    documents for the year."""
    import pack_eval as P
    docs = P.year_docs(cur, wba, year, fy)
    if not docs:
        return [], [], {}
    pages = P.candidate_pages(cur, [d[0] for d in docs], queries)
    pas = P.cut(pages, "narrative")
    if not pas:
        return [], [], {d[0]: (d[1], d[2]) for d in docs}
    words = R.query_terms(" ".join(queries))
    extra = terms_for(model, code, fold)
    idfs = dict((model or {}).get("idf") or {})
    for t in list(words) + extra:
        idfs.setdefault(t, 1.0)          # a word the training never saw still counts
    scores = P.score_passages(pas, words, extra, idfs, (model or {}).get("avg") or 1.0)
    chosen = P.pick(pas, scores, (model or {}).get("pack") or P.PACK, (model or {}).get("per_page") or P.PER_PAGE)
    return pas, chosen, {d[0]: (d[1], d[2]) for d in docs}
