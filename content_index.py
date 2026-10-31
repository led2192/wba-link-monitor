#!/usr/bin/env python3
"""
content_index.py: the report's own index as a retrieval signal.

A sustainability report ends with a GRI or ESRS content index, a table that names each disclosure and
the page it is on ("305-1 Direct (Scope 1) GHG emissions 84", "E1-6 Gross Scopes 1, 2, 3 ... 112-115"),
and starts with a table of contents. That is another page of the same report saying where the figure is:
the equivalent of the anchor text early web search leaned on, and the strongest signal the page ranking
did not use (31 Oct 2026; 42 of the 46 pages missed on 29 Oct carried no label at all, which is exactly
the case an index entry fixes).

For an attribute, attributes.json gives the disclosure codes and the words its index rows use ("index":
{"codes": ["305-1", "E1-6"], "terms": ["direct (scope 1)", "scope 1"]}). This module

  1. finds the index pages of the year's documents (pages dense in GRI or ESRS codes, or that say
     "content index", with page numbers on them);
  2. reads the printed page numbers the matching rows cite ("84", "84-86", "p. 84", "84, 112");
  3. turns printed pages into PDF pages with the document's own offset, estimated from the page numbers
     printed at the top or bottom of its pages (the mode of pdf page minus printed page), and when that
     is unknown, a spread of the usual offsets at half weight;
  4. returns {(library_id, pdf page): weight in 0..1} for extract_eval to add to the page score and to
     bring into the pool pages the label search never offered.

Pure functions are tested offline (tests/test_content_index.py); the two database functions only read.
"""
import collections, re

MAX_INDEX_PAGES = 40                 # index pages read per set of documents
MAX_CITED = 6                        # cited pages kept per attribute and document
OFFSETS_UNKNOWN = (0, 1, 2, 3, 4)    # pdf page minus printed page, when the document does not say
GRI_CODE = re.compile(r"(?<![\w-])(?:GRI\s*)?(\d{3}-\d{1,2}|2-\d{1,2})(?![\w-])")
ESRS_CODE = re.compile(r"(?<![\w-])([ESG]\d-\d{1,2}|ESRS\s?2|GOV-\d|SBM-\d|IRO-\d|BP-\d)(?![\w-])", re.I)
INDEX_WORDS = re.compile(r"content index|gri index|gri standard|esrs index|índice de contenido|index des contenus|inhaltsindex|"
                         r"table of contents|contents\b", re.I)
PAGE_REF = re.compile(r"(?<![\d-])(?<!\d[.,])(\d{1,3})(?:\s*[-–]\s*(\d{1,3}))?(?![\d%-])(?![.,]\d)")   # 84, 84-86; not 12.5 or 1,234
TRAILING = re.compile(r"((?:p{1,2}\.?\s*)?\d{1,3}(?:\s*[-–]\s*\d{1,3})?(?:\s*(?:,|;|and|&)\s*(?:p{1,2}\.?\s*)?\d{1,3}(?:\s*[-–]\s*\d{1,3})?)*)\s*$", re.I)
NOT_PAGES = re.compile(r"\(\s*scopes?\s*\d(?:\s*,\s*\d)*(?:\s*(?:and|&)\s*\d)?\s*\)|\bscopes?\s*\d(?:\s*,\s*\d)*(?:\s*(?:and|&)\s*\d)?|"
                       r"\b(?:gri|esrs|sdg|tier|level|phase|pillar|category|cat\.)\s*\d+|\b(?:19|20)\d{2}\b", re.I)
STANDALONE = re.compile(r"(?:^|\n)\s*(\d{1,3})\s*(?:\n|$)|(?:^|\n)\s*(\d{1,3})\s+\S|\S\s+(\d{1,3})\s*(?:\n|$)")


def is_index_page(body):
    """A content index or table of contents: many disclosure codes, or the index words, with page numbers."""
    b = body or ""
    codes = len(GRI_CODE.findall(b)) + len(ESRS_CODE.findall(b))
    refs = len(PAGE_REF.findall(b))
    return (codes >= 4 and refs >= 6) or (bool(INDEX_WORDS.search(b)) and refs >= 12 and codes >= 1)


def cited_printed_pages(body, codes, terms):
    """Printed page numbers the index rows for these codes or terms cite, most cited first. A row is a line
    of the page's text; numbers are taken after the match (a range 84-86 gives 84, 85, 86), never the
    code's own digits or a four-digit year."""
    found = collections.Counter()
    code_rx = [re.compile(r"(?<![\w-])" + re.escape(c) + r"(?![\w-])", re.I) for c in codes or []]
    term_rx = [re.compile(re.escape(t), re.I) for t in terms or []]
    for line in (body or "").split("\n"):
        if not line.strip():
            continue
        pos = None
        for rx in code_rx + term_rx:
            m = rx.search(line)
            if m and (pos is None or m.end() < pos):
                pos = m.end()
        if pos is None:
            continue
        tail = GRI_CODE.sub(" ", ESRS_CODE.sub(" ", line[pos:]))     # other codes on the same line are not pages
        for rx in term_rx:
            tail = rx.sub(" ", tail)                                       # "Scope 1" inside the row's own words
        tail = NOT_PAGES.sub(" ", tail)                                    # "(Scope 1)", "GRI 305", a standard's year
        m = TRAILING.search(tail)
        if m:                                                              # the page column closes the row
            tail = m.group(1)
        for a, b in PAGE_REF.findall(tail)[:4]:
            a = int(a)
            if not 1 <= a <= 999:
                continue
            b = int(b) if b else a
            pages = range(a, b + 1) if a <= b <= a + 6 else [a]
            for p in pages:
                found[p] += 1
    return [p for p, _ in found.most_common(MAX_CITED)]


def printed_offset(samples):
    """pdf page minus printed page for a document, from (pdf page, first characters, last characters) of its
    pages: the mode of the offsets a standalone number at the top or bottom of a page gives, when at least
    three pages agree and it is between 0 and 12; else None."""
    votes = collections.Counter()
    for pdf, head, tail in samples:
        for seg in (head or "", tail or ""):
            for m in STANDALONE.finditer(seg):
                n = int(next(g for g in m.groups() if g))
                if 0 <= pdf - n <= 12:
                    votes[pdf - n] += 1
                    break
    if not votes:
        return None
    off, k = votes.most_common(1)[0]
    return off if k >= 3 else None


def pdf_pages_for(printed, offset, page_count=None):
    """{pdf page: weight} for one cited printed page: the known offset at full weight, else the usual
    offsets at half weight."""
    out = {}
    offs = [(offset, 1.0)] if offset is not None else [(o, 0.5) for o in OFFSETS_UNKNOWN]
    for o, w in offs:
        p = printed + o
        if p >= 1 and (page_count is None or p <= page_count):
            out[p] = max(out.get(p, 0), w)
    return out


# ---------------------------------------------------------------------------------------------- database
def index_pages(cur, ids):
    """[(library_id, body)] of the index pages among these documents."""
    cur.execute("select library_id, body from public.pages where library_id = any(%s) and "
                "words @@ to_tsquery('simple', 'gri | esrs | index | contents') limit 400", (ids,))
    out = []
    for lid, body in cur.fetchall():
        if is_index_page(body):
            out.append((lid, body))
            if len(out) >= MAX_INDEX_PAGES:
                break
    return out


def document_offset(cur, lid):
    """The document's pdf-minus-printed page offset, or None."""
    cur.execute("select page, left(body, 60), right(body, 60) from public.pages where library_id = %s order by page", (lid,))
    return printed_offset(cur.fetchall())


def cited(cur, ids, attr):
    """{(library_id, pdf page): weight} of the pages the documents' own indexes cite for the attribute."""
    spec = attr.get("index") or {}
    if not (spec.get("codes") or spec.get("terms")):
        return {}
    out = {}
    offsets = {}
    for lid, body in index_pages(cur, ids):
        printed = cited_printed_pages(body, spec.get("codes"), spec.get("terms"))
        if not printed:
            continue
        if lid not in offsets:
            offsets[lid] = document_offset(cur, lid)
        for p in printed:
            for pdf, w in pdf_pages_for(p, offsets[lid]).items():
                out[(lid, pdf)] = max(out.get((lid, pdf), 0), w)
    return out
