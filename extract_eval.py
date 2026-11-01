#!/usr/bin/env python3
"""
extract_eval.py: the generic reader of number attributes, scored against the validated answers.

Everything attribute-specific comes from attributes.json (labels, exclusions, document types, unit family
and scales); the code is the same for every attribute, so a methodology change is an edit of that file and a
rerun of this script (owner's rule, 29 Oct 2026). No model, unless --reader model: then the stages up to
the pages are the same and a language model reads the best MODEL_PAGES pages (model_reader.py).

For every validated 2025-benchmark answer of a number attribute, the reader works as an extractor would,
without looking at the answer, and each stage is scored against it:

  1. documents     the company's documents of the answer's year or the next, the spec's doc_types first;
  2. pages         in up to MAX_DOCS of them, the pages whose words match the attribute's labels, best
                   ranked first (up to MAX_PAGES);
  3. reading       on each page in that order, the first table row or sentence where a label (not next to
                   an excluded word) is followed by numbers: the column of the answer's year when the page
                   prints a year header, else the first number; the scale from the page ("in millions",
                   "€m", "'000", "ktCO2e"); the first page that gives a value wins;
  4. score         the proposal is right when within MATCH (0.5 %) of the validated value.

Reported per attribute, as shares of the answers with a usable value: a document for the year; the
validated number on one of the chosen documents' pages (the ceiling of this route); the number on one of
the ranked pages (what the reader was given); a proposal; a right proposal (and right among proposals);
the right digits at the wrong scale (a scale-reading miss, the cheapest to fix).

Read-only: the gold bases (GOLD_TOKEN), the evidence database (SUPABASE_DB_URL). Writes extract_eval.md
(also a run artifact) and the run's counters in Pipeline QA. Options: --limit N answers per attribute
(dry run default 40), --attributes codes, --budget minutes (100), --reader rules|model, --commit.
"""
import argparse, collections, json, os, re, sys, time, traceback

import qa
from value_eval import CURRENCY_SCALES, EMISSION_SCALES, found_on, printed_forms, scales_for, tsquery_for

SPEC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "attributes.json")
MAX_DOCS, MAX_PAGES = 12, 25         # documents of the year searched; pages kept after ranking
PER_DOC = 10                         # at most this many of the kept pages from one document
POOL = 400                           # label-matching pages scored before ranking
MODEL_PAGES = 8                      # pages given to the model reader: half by the rule candidates, half by rank
MORE_TERMS_TIMEOUT = "4s"            # the second query (an attribute's more_terms) may take this long, else it is skipped
MORE_POOL = 30                       # pages it may add (bodies are read for these only)
INDEX_WEIGHT = 1.5                   # page score added to a page the report's own content index cites (content_index.py)
BIGNUM = re.compile(r"\d{1,3}(?:[,.'\u00a0\u202f ]\d{3})+|\d{4,}")
MATCH = 0.005
# a printed number: Indian grouping, comma/dot/apostrophe groups, space groups (never followed by another
# separator and three digits: "7 102,502" is a note 7 then 102,502), or plain digits; an optional decimal part
NUM = re.compile(r"\(?-?(?:\d{1,2}(?:,\d{2})+,\d{3}(?:\.\d+)?|\d{1,3}(?:[,.'’]\d{3})+(?:[.,]\d+)?"
                 r"|\d{1,3}(?:[   ]\d{3})+(?![\d]|[,.]\d)|\d+(?:[.,]\d+)?)\)?")
YEAR = re.compile(r"(?<![\dA-Za-z])(?:FY\s?|FY'|')?((?:19|20)\d{2}|(?<=FY)\d{2}|(?<=FY )\d{2})(?!\d)")


# ----------------------------------------------------------------------------------------------
# Pure helpers: reading a page
# ----------------------------------------------------------------------------------------------
def to_number(t):
    """'1,234.5' -> 1234.5; '1.234,5' -> 1234.5; '7,50,616' -> 750616; '65 534' -> 65534; '(12)' -> 12."""
    t = t.strip("()-").replace(" ", " ").replace(" ", " ").replace("’", "'")
    seps = re.findall(r"[,. ']", t)
    if not seps:
        return float(t)
    last = seps[-1]
    head, _, tail = t.rpartition(last)
    digits = re.sub(r"[,. ']", "", head)
    if last in ",." and len(tail) != 3:
        return float(digits + "." + tail)
    if last in ",." and len(seps) == 1 and len(tail) == 3 and len(head) > 3:
        return float(digits + "." + tail)
    return float(digits + tail)


def numbers_after(text, start, limit=160):
    """The numbers that follow a label, in order, until the next word (a row label) or `limit` chars.
    Years and small note references are left out."""
    seg = text[start:start + limit]
    out = []
    pos = 0
    for m in NUM.finditer(seg):
        gap = seg[pos:m.start()]
        if re.search(r"[A-Za-zÀ-ÿ]{4,}", gap) and out:          # the next row's label
            break
        tok = m.group(0)
        pos = m.end()
        try:
            v = to_number(tok)
        except ValueError:
            continue
        if re.fullmatch(r"(?:19|20)\d{2}", tok):               # a year
            continue
        if re.match(r"[./-]\d{2,4}", seg[m.end():m.end() + 5]) or re.search(r"\d[./-]$", seg[max(0, m.start() - 2):m.start()]):
            continue                                          # part of a date: 12.31.2023, 31/12/2024
        if v < 100 and "." not in tok and "," not in tok and not out and len(seg) > m.end() + 2 and NUM.search(seg[m.end():m.end() + 25]):
            continue                                          # a note number before the figures
        if re.match(r"\s?%", seg[m.end():m.end() + 2]):          # a percentage
            continue
        out.append((v, tok, m.start()))
    return out


def year_header(text, year):
    """Index of the answer's year in the first run of two or more years printed side by side (the table's
    column header), trying the year and the next (a fiscal year labelled by its end); None if no header."""
    for m in re.finditer(r"((?:(?:FY\s?)?(?:(?:19|20)\d{2}|(?<=FY)\d{2}|(?<=FY )\d{2})\s*[/|]?\s*){2,})", text):
        ys = [int(y) + (2000 if len(y) == 2 else 0) for y in YEAR.findall(m.group(1))]
        for y in (year, year + 1):
            if y in ys and len(set(ys)) == len(ys):
                return ys.index(y)
    return None


def page_scale(text, family, spec):
    """The scale the page states for its figures (1 when it says nothing)."""
    t = text.lower().replace("’", "'")
    if family == "tco2e":
        for pat, f in ((r"\bmt\s?co2|million (?:metric )?ton", 1e6), (r"\bkt\s?co2|thousand (?:metric )?ton|'000\s?t|000\s?tco2", 1e3)):
            if re.search(pat, t):
                return f
        return 1
    if family == "count":
        return 1e3 if re.search(r"\b(?:in thousands|thousands of (?:employees|people)|'000 (?:employees|people))\b", t) else 1
    scales = spec["unit_families"]["currency"]["scales"]
    pats = [
        r"(?:in|of|units?:?)\s+(?:[a-z]{3}\s+|[$€£¥₹]\s?)?(thousands?|millions?|billions?|lakhs?|crores?|mn|bn|mio|mrd)\b",
        r"\(\s*(?:[a-z]{2,3}\s*|[$€£¥₹]\s?)?(thousands?|millions?|billions?|lakhs?|crores?|mn|bn|mio|mrd|m|k|'000|000)\s*\)",
        r"(?:[$€£¥₹]|\b(?:usd|eur|gbp|chf|jpy|cny|rmb|inr|cad|aud|sek|nok|dkk|brl|zar|r))\s?('000|000|m|mn|bn|k)\b",
        r"(百万円|百万|億円|亿)",
        r"\b(t|k)(?:eur|usd|chf|gbp|sek|nok|dkk|pln)\b",          # TEUR, kUSD: thousands
        r"\b(m)(?:eur|usd|chf|gbp|sek|nok|dkk|pln)\b",            # MEUR, MSEK: millions
        r"\b(?:sek|nok|dkk|pln|huf|czk)\s?(m|mn|bn|k)\b",
        r"\b(thousands?|millions?|billions?) of\b",
    ]
    for p in pats:
        m = re.search(p, t)
        if m:
            k = m.group(1).strip()
            if k in scales:
                return scales[k]
    return 1


def label_positions(text, labels, exclude):
    """(position after the label, label) for every label occurrence not next to an excluded word, the
    attribute's most specific labels first."""
    t = text.lower()
    out = []
    for lab in labels:
        pat = r"[-\s]+".join(re.escape(w) for w in re.split(r"[-\s]+", lab.lower()))
        for m in re.finditer(r"(?<![a-z])" + pat + r"(?![a-z])", t):
            ctx = t[max(0, m.start() - 30):m.end() + 30]
            if any(x in ctx for x in exclude):
                continue
            out.append((m.end(), lab))
    return out


NARRATIVE = re.compile(r"\b(?:reduc\w*|target\w*|goal|aim\w*|commit\w*|by 20\d\d|compared|versus|increase\w*|decrease\w*)\b")


def candidates(text, year, attr, spec):
    """[(score, value, raw, scale, label)] for every label mention followed by numbers on a page. The first
    mention is often prose ("Scope 1 and 2", "reduce Scope 2 by 2030"); the figure is in a table row. So
    every mention is scored (sample of 29 Oct 2026: reading the first mention was right for 0 to 8 % of
    answers): up for a table row (several numbers, thousands separators or four digits or more, a year
    header on the page, the number right after the label, a specific label), down for small integers and
    prose around it."""
    family = attr.get("unit_family", "currency")
    labels = attr.get("labels", [])
    col = year_header(text, year)
    scale = page_scale(text, family, spec)
    low = text.lower()
    out = []
    for pos, lab in label_positions(text, labels, attr.get("exclude", [])):
        nums = numbers_after(text, pos)
        if not nums:
            continue
        v, tok, off = nums[col] if col is not None and col < len(nums) else nums[0]
        digits = len(re.sub(r"\D", "", re.split(r"[.,](?=\d{1,2}$)", tok)[0]))
        score = 2.0 * (len(labels) - labels.index(lab)) / len(labels)
        score += 1.0 if len(nums) >= 2 else 0.0
        score += 0.5 if len(nums) >= 3 else 0.0
        score += 2.0 if re.search(r"\d[,.' \u00a0\u202f]\d{3}", tok) or digits >= 4 else 0.0
        score += 1.0 if col is not None else 0.0
        score += 1.0 if nums[0][2] <= 20 else 0.0
        score -= 3.0 if v < 10 and not re.search(r"[.,]\d", tok) else 0.0
        score -= 1.0 if v < 100 and digits <= 2 else 0.0
        score -= 2.0 if NARRATIVE.search(low[pos:pos + 80]) else 0.0
        out.append((score, v * scale, v, scale, lab))
    return out


def propose(text, year, attr, spec):
    """(value, raw number, scale, label) of the best-scored candidate on a page, or None."""
    c = candidates(text, year, attr, spec)
    if not c:
        return None
    best = max(c, key=lambda x: x[0])
    return best[1:]


def right(v, gold):
    return bool(v) and bool(gold) and abs(v - gold) / gold <= MATCH


def label_variants(labels):
    """The labels plus their spelling with spaces for hyphens and hyphens for spaces between two words
    ("location-based" and "location based"): reports print both, and the word index treats them as
    different words. On 29 Oct 2026, 10 of the 11 Scope 2 location-based answers whose page was missed
    printed "location based" with a space."""
    out = []
    for l in labels:
        for v in (l, l.replace("-", " "), re.sub(r"(?<=[a-z]) (?=based\b|aligned\b)", "-", l)):
            if v not in out:
                out.append(v)
    return out


MAX_QUERY_LABELS = 30                # a guard on the phrases in one word-index query; what slowed the capex search (31 Oct 2026)
                                     # was phrases of common words, which now go to a separate query (more_terms_pages)


def label_query(labels):
    """websearch query: any of the labels (and their hyphen variants), each as a phrase; the first
    MAX_QUERY_LABELS variants only (labels are listed most specific first), because every phrase makes the
    index recheck its candidate pages and a long list times out."""
    labels = label_variants(labels)[:MAX_QUERY_LABELS]
    ascii_labels = [l for l in labels if re.fullmatch(r"[\w\s\-']+", l, flags=re.ASCII)]
    return " or ".join(f'"{l}"' if " " in l else l for l in ascii_labels) or "zzzz"


def page_score(text_rank, cand, body, year, labels, exclude, doc_weight, index_boost=0.0):
    """How likely a page holds the attribute's figure for the year, from five signals, each in 0 to 1:
    the label match of the word index, the best rule candidate on the page, how much of a table it is
    (numbers of four digits or more), whether it prints the year (or its fiscal label), how often the
    labels occur, and the document's type and year. Documents first, then pages within them, lost pages
    (29 Oct 2026: the number was in the year's documents for 60 to 80 % of answers, on the 25 kept pages
    for 30 to 60 %), because a long annual report filled all 25 places with pages that merely name the
    attribute."""
    tab = min(len(BIGNUM.findall(body or "")), 30) / 30
    y1 = str(year + 1)
    has_year = 1.0 if (str(year) in (body or "") or f"FY{y1[2:]}" in (body or "") or f"FY{y1}" in (body or "")) else 0.0
    hits = min(len(label_positions(body or "", labels, exclude)), 5) / 5
    c = max(cand, 0) / 8 if cand is not None else 0.0
    return 1.0 * text_rank + 1.0 * min(c, 1) + 0.5 * tab + 0.5 * has_year + 0.5 * hits + 0.7 * doc_weight + INDEX_WEIGHT * index_boost


def retrieval_miss(holding, rows, forms):
    """Why the pages that print the validated number were not kept: {"kind", "rank", "phrases"}.
    kind: "no label on the page" (the word index never offered it), "label on the next or previous page"
    (a table split over two pages), or "ranked too low" (offered, scored below the kept pages; rank = its
    best position). phrases: the words printed just before the number on those pages, for new labels."""
    from label_mining import label_window, number_spans, phrases
    order = {(r[1], r[2]): i for i, r in enumerate(sorted(rows, key=lambda r: -r[0]))}
    ranked = [order[(lid, p)] for lid, p, _ in holding if (lid, p) in order]
    near = [p for lid, p, _ in holding if (lid, p - 1) in order or (lid, p + 1) in order]
    ph = set()
    for _, _, body in holding:
        for v, _, d in forms:
            for s in number_spans(body, v, d):
                ph |= phrases(label_window(body, s))
    if ranked:
        return {"kind": "ranked too low", "rank": min(ranked) + 1, "phrases": ph}
    if near:
        return {"kind": "label on the next or previous page", "rank": None, "phrases": ph}
    return {"kind": "no label on the page", "rank": None, "phrases": ph}


def keep_pages(rows, n=MAX_PAGES, per_doc=PER_DOC):
    """The n best-scored pages, at most per_doc from one document. rows: [(score, library_id, ...)]."""
    out, per = [], collections.Counter()
    for r in sorted(rows, key=lambda r: -r[0]):
        if per[r[1]] >= per_doc:
            continue
        out.append(r); per[r[1]] += 1
        if len(out) >= n:
            break
    return out


def pages_for_model(scored, n):
    """Up to n of the ranked pages for the model: first the pages with the best rule candidates (half of n,
    those that have a candidate), then the best-ranked pages not yet taken. scored: [(score, candidate or
    None, library_id, page, body)] in rank order. On the first sample (29 Oct 2026) the validated number was
    on the 3 best-candidate pages for 34 % of the answers and on the 25 ranked pages for 50 to 60 %."""
    by_candidate = sorted((x for x in scored if x[1]), key=lambda x: -x[0])[:max(1, n // 2)]
    out = list(by_candidate)
    for x in scored:
        if len(out) >= n:
            break
        if x not in out:
            out.append(x)
    return out


# ----------------------------------------------------------------------------------------------
def year_documents(cur, wba, year, attr):
    """([(library_id, doc_type, year)], {library_id: weight}): the company's documents of the year or the
    next, preferred types first, at most MAX_DOCS; the weight favours the preferred types and the year itself."""
    ranks = {t: i for i, t in enumerate(attr.get("doc_types", []))}
    cur.execute("select library_id, coalesce(doc_type, ''), coalesce(year, 0) from public.documents "
                "where wba_id = %s and year in (%s, %s)", (wba, year, year + 1))
    docs = sorted(cur.fetchall(), key=lambda d: (ranks.get(d[1], 99), d[2] != year, d[0]))[:MAX_DOCS]
    nt = max(len(ranks), 1)
    weight = {d[0]: (1 - ranks[d[1]] / nt if d[1] in ranks else 0.0) * (1.0 if d[2] == year else 0.7) for d in docs}
    return docs, weight


def ranked_pages(cur, ids, weight, year, attr, spec, info=None):
    """[(score, library_id, page, body, best rule candidate)] for the label-matching pages of the documents,
    plus the pages the documents' own content index cites for the attribute (content_index.py, 31 Oct 2026),
    which get INDEX_WEIGHT and join the pool even when no label is on them. info, a dict when given,
    receives "cited": {(library_id, page): weight}."""
    import content_index
    cited = content_index.cited(cur, ids, attr)
    if info is not None:
        info["cited"] = dict(cited)
    cur.execute("select library_id, page, body, ts_rank_cd(words, websearch_to_tsquery('simple', %s), 32) as rk from public.pages "
                "where library_id = any(%s) and words @@ websearch_to_tsquery('simple', %s) "
                "order by rk desc limit %s",
                (label_query(attr["labels"]), ids, label_query(attr["labels"]), POOL))
    found = cur.fetchall()
    found += more_terms_pages(cur, ids, attr, {(lid, page) for lid, page, _, _ in found}, info)
    missing = set(cited) - {(lid, page) for lid, page, _, _ in found}
    if missing:
        cur.execute("select library_id, page, body from public.pages where library_id = any(%s) and page = any(%s)",
                    (sorted({l for l, _ in missing}), sorted({p for _, p in missing})))
        found += [(lid, page, body, 0.0) for lid, page, body in cur.fetchall() if (lid, page) in missing]
    rows = []
    for lid, page, body, rk in found:
        cs = candidates(body, year, attr, spec)
        top = max(cs, key=lambda c: c[0]) if cs else None
        sc = page_score(rk, top[0] if top else None, body, year, attr.get("labels", []) + attr.get("more_terms", []),
                        attr.get("exclude", []), weight.get(lid, 0), cited.get((lid, page), 0.0))
        rows.append((sc, lid, page, body, top))
    return rows


STOPWORDS = {"in", "of", "the", "and", "for", "to", "a", "an", "on"}


def words_query(terms):
    """to_tsquery text: any of the terms, each as its words with AND and no word order ("investments in energy
    efficiency" -> investments & energy & efficiency). Word order is what makes a phrase slow to check in the
    word index (32 of 38 capex answers timed out with phrases, 31 Oct 2026); the order is checked afterwards in
    Python, on the pages found."""
    parts = []
    for t in terms:
        ws = [w for w in re.findall(r"[a-z0-9]+", t.lower()) if w not in STOPWORDS]
        if ws:
            parts.append("(" + " & ".join(ws) + ")")
    return " | ".join(parts) or "zzzz"


def more_terms_pages(cur, ids, attr, have, info=None):
    """[(library_id, page, body, rank)] of the pages that print one of the attribute's more_terms and were not
    found by the label query. A separate query with its own MORE_TERMS_TIMEOUT inside a savepoint: wording
    with common words ("investments in energy efficiency") is slow to check in the word index, and adding it
    to the label query timed out the whole search (31 Oct 2026); here a slow query only loses its own pages.
    info["more_terms"] receives "ok", "timeout" or "none"."""
    terms = attr.get("more_terms") or []
    if not terms:
        return []
    q = words_query(terms)
    cur.execute("show statement_timeout")
    before = cur.fetchone()[0]
    try:
        with cur.connection.transaction():
            cur.execute(f"set local statement_timeout = '{MORE_TERMS_TIMEOUT}'")
            # The company's pages first (the primary key starts with library_id), then the words: left to itself
            # the planner took the word index, whose OR of common words ("capital", "energy") matches millions of
            # pages before the library_id filter, and the scoring passed its two-hour limit (31 Oct 2026). Bodies
            # are read only for the MORE_POOL best pages.
            cur.execute("with p as materialized (select library_id, page, words from public.pages where library_id = any(%s)), "
                        "m as (select library_id, page, ts_rank_cd(words, to_tsquery('simple', %s), 32) as rk from p "
                        "      where words @@ to_tsquery('simple', %s) order by rk desc limit %s) "
                        "select m.library_id, m.page, b.body, m.rk from m join public.pages b using (library_id, page) "
                        "order by m.rk desc", (ids, q, q, MORE_POOL))
            rows = [r for r in cur.fetchall() if (r[0], r[1]) not in have and label_positions(r[2] or "", terms, [])]
        if info is not None:
            info["more_terms"] = "ok"
        return rows
    except Exception:
        if info is not None:
            info["more_terms"] = "timeout"
        return []
    finally:
        try:
            cur.execute(f"set statement_timeout = '{before}'")
        except Exception:
            pass


def read_values(cur, wba, year, attr, spec, usage):
    """Every figure the model reads for a company and attribute from the documents of one year, with no
    validated answer needed: the same documents, ranking and pages as evaluate(). The year asked comes first
    when the pages print it; the other entries are the other years the same pages print (one call for all,
    31 Oct 2026). Each entry: value, printed, scale, library_id, page, unit, year, column, and "asked" (the
    year whose documents were read: when it differs from "year", the figure is a comparison column of a
    later report). [] when nothing was read."""
    from model_reader import read_years
    docs, weight = year_documents(cur, wba, year, attr)
    if not docs:
        return []
    pages = keep_pages(ranked_pages(cur, [d[0] for d in docs], weight, year, attr, spec))
    scored = [((top[0] if top else -99) - 0.05 * i, top, lid, page, body) for i, (_, lid, page, body, top) in enumerate(pages)]
    best = pages_for_model(scored, MODEL_PAGES)
    if not best:
        return []
    got = read_years({"wba": wba, "year": year}, attr, [(lid, page, body) for _, _, lid, page, body in best], usage)
    for e in got:
        e["asked"] = year
    return got


def read_value(cur, wba, year, attr, spec, usage):
    """What the model reader proposes for a company, attribute and year: the entry of read_values for that
    year (the others under "others"), or None."""
    got = read_values(cur, wba, year, attr, spec, usage)
    mine = next((e for e in got if e["year"] == year), None)
    if mine:
        mine["others"] = [e for e in got if e is not mine]
    return mine


def evaluate(cur, g, attr, spec, reader="rules", usage=None):
    """Stage results for one answer. reader "rules": the best-scored labelled row on the ranked pages;
    "model": a language model reads MODEL_PAGES pages: the ones whose rule candidates scored best, then the
    best-ranked ones not yet taken (pages_for_model)."""
    r = {"docs": False, "in_docs": False, "in_pages": False, "proposal": False, "right": False, "scale_miss": False}
    docs, weight = year_documents(cur, g["wba"], g["year"], attr)
    if not docs:
        return r
    r["docs"] = True
    ids = [d[0] for d in docs]
    scales = scales_for(attr.get("unit_family"))
    forms = printed_forms(g["value"], scales)
    if forms:
        q = " | ".join(f"({tsquery_for(v, d)})" for v, _, d in forms)
        cur.execute("select library_id, page, body from public.pages where library_id = any(%s) and words @@ to_tsquery('simple', %s) limit 40", (ids, q))
        holding = [(lid, page, body) for lid, page, body in cur.fetchall() if any(found_on(body, v) for v, _, _ in forms)]
        r["in_docs"] = bool(holding)
    else:
        holding = []
    info = {}
    rows = ranked_pages(cur, ids, weight, g["year"], attr, spec, info)
    pages = keep_pages(rows)
    r["in_pages"] = any(found_on(p[3], v) for p in pages for v, _, _ in forms)
    r["more_terms"] = info.get("more_terms")
    r["index_cited"] = bool(info.get("cited"))                       # the report's own index named a page
    r["index_hit"] = any((lid, page) in info.get("cited", {}) for lid, page, _ in holding)
    if r["in_docs"] and not r["in_pages"]:
        r["miss"] = retrieval_miss(holding, rows, forms)
    # each kept page's best rule candidate, used by the rule reader and to pick the model's pages
    scored = [((top[0] if top else -99) - 0.05 * i, top, lid, page, body) for i, (_, lid, page, body, top) in enumerate(pages)]
    r["model_pages_have_number"] = any(found_on(b, v) for *_, b in pages_for_model(scored, MODEL_PAGES) for v, _, _ in forms)
    if reader == "model":
        from model_reader import read_years
        best_pages = pages_for_model(scored, MODEL_PAGES)
        note = {}
        all_got = read_years(g, attr, [(lid, page, body) for _, _, lid, page, body in best_pages], usage, note=note)
        got = next((e for e in all_got if e["year"] == g["year"]), None)
        r["others"] = [e for e in all_got if e is not got]        # the other years the same call read
        r["declined_why"] = note.get("why") or ("(only other years: " + ", ".join(str(e["year"]) for e in all_got) + ")" if all_got and not got else None)
        if got:
            r["proposal"] = True
            r["right"] = right(got["value"], g["value"])
            r["scale_miss"] = not r["right"] and any(right(got["printed"] * s, g["value"]) for s in scales)
            r["got"] = ((got["value"], got["printed"], got["scale"], "the model"), got["library_id"], got["page"])
        return r
    best = max((x for x in scored if x[1]), key=lambda x: x[0], default=None)
    if best:
        got = best[1][1:]
        r["proposal"] = True
        r["right"] = right(got[0], g["value"])
        r["scale_miss"] = not r["right"] and any(right(got[1] * s, g["value"]) for s in scales)
        r["got"] = (got, best[2], best[3])
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--attributes", default="")
    ap.add_argument("--budget", type=int, default=100)
    ap.add_argument("--reader", default="rules", choices=["rules", "model"])
    a = ap.parse_args()
    if a.reader == "model" and not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        open("extract_eval.md", "w").write("### Extraction: the model reader needs ANTHROPIC_API_KEY (a repository secret); nothing run.\n")
        return 0
    usage = collections.Counter()
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    limit = a.limit or (0 if a.commit else 40)
    spec = json.load(open(SPEC, encoding="utf-8"))
    gold_cfg = json.load(open(os.path.join(os.path.dirname(SPEC), "gold_attributes.json"), encoding="utf-8"))
    codes = [c for c, v in spec["attributes"].items() if v["type"] == "number" and (not a.attributes or c in a.attributes.split(","))]
    token = os.environ.get("GOLD_TOKEN", "").strip()
    if not token:
        open("extract_eval.md", "w").write("### Extraction: GOLD_TOKEN is not set; nothing run.\n")
        return 0
    import psycopg
    from value_eval import gold_values
    run = qa.Run("extract_eval.py")
    answers = [g for base in gold_cfg["bases"].values() for g in gold_values(token, base, codes)
               if g["value"] and not g["unit"].lower().startswith(("%", "percent"))]
    gold_index = {(g["wba"], g["attribute"], g["year"]): g["value"] for g in answers}
    by = collections.defaultdict(list)
    for g in answers:
        by[g["attribute"]].append(g)
    queues = [by[c][:limit] if limit else by[c] for c in codes]
    todo = [q[i] for i in range(max((len(q) for q in queues), default=0)) for q in queues if i < len(q)]
    print(f">>> {len(todo):,} answers to read ({', '.join(f'{c} {len(q)}' for c, q in zip(codes, queues))})", flush=True)

    stats = {c: collections.Counter() for c in codes}
    declined = []
    miss_ranks = collections.defaultdict(list)
    miss_phrases = {c: collections.Counter() for c in codes}
    wrong = collections.defaultdict(list)
    t0, cut = time.time(), False
    with psycopg.connect(os.environ["SUPABASE_DB_URL"].strip(), connect_timeout=30) as conn:
        cur = conn.cursor()
        cur.execute("set statement_timeout = '30s'")
        for i, g in enumerate(todo, 1):
            if time.time() - t0 > a.budget * 60:
                cut = True; break
            if i % 100 == 0:
                print(f"  {i:,}/{len(todo):,} in {(time.time() - t0) / 60:.0f} min", flush=True)
            s = stats[g["attribute"]]; s["answers"] += 1
            try:
                r = evaluate(cur, g, spec["attributes"][g["attribute"]], spec, a.reader, usage)
            except Exception as e:
                if type(e).__name__ == "ModelRefused":           # the API said no: stop and say why
                    raise
                conn.rollback(); cur.execute("set statement_timeout = '30s'")
                s["database did not answer"] += 1; continue
            for k in ("docs", "in_docs", "in_pages", "proposal", "right", "scale_miss"):
                s[k] += bool(r[k])
            s["model_pages"] += bool(r.get("model_pages_have_number"))
            s["index cited"] += bool(r.get("index_cited")); s["index hit"] += bool(r.get("index_hit"))
            if r.get("more_terms"):
                s["more terms " + r["more_terms"]] += 1
            for e in r.get("others") or []:                   # the other years the same call read, scored where validated
                s["other years"] += 1
                gv = gold_index.get((g["wba"], g["attribute"], e["year"]))
                if gv:
                    s["other years validated"] += 1
                    s["other years right"] += right(e["value"], gv)
            if r.get("miss"):
                s["miss: " + r["miss"]["kind"]] += 1
                if r["miss"]["rank"]:
                    miss_ranks[g["attribute"]].append(r["miss"]["rank"])
                if r["miss"]["kind"] != "ranked too low":
                    miss_phrases[g["attribute"]].update(r["miss"]["phrases"])
            if r.get("model_pages_have_number") and not r["proposal"]:
                s["declined with the number on its pages"] += 1
                if len(declined) < 20:
                    declined.append(f"{g['id']}: \"{r.get('declined_why') or 'no reason given'}\"")
            if r["proposal"] and not r["right"] and len(wrong[g["attribute"]]) < 12:
                (v, raw, sc, lab), lid, page = r["got"]
                wrong[g["attribute"]].append(f"{g['id']}: validated {g['value']:,.2f} {g['unit']}; read {raw:,.2f} × {sc:g} "
                                             f"after \"{lab}\" ({lid} p.{page})")

    title = "model reader (" + os.environ.get("READER_MODEL", "claude-haiku-5-5") + ")" if a.reader == "model" else "generic reader (no model)"
    md = [f"### The {title} against the validated answers" + ("" if a.commit else " (dry run, sample)"), "",
          f"{sum(s['answers'] for s in stats.values()):,} answers with a value" + (f" ({limit} per attribute)" if limit else "")
          + ("; stopped at the time budget" if cut else "") + ". Shares of those answers; the reader never sees the answer.", "",
          "| Attribute | Answers | Document for the year | Number in those documents | Number on the ranked pages | Number on the model's pages | Proposal | Right | Right among proposals | Right digits, wrong scale |",
          "| --- | --: | --: | --: | --: | --: | --: | --: | --: | --: |"]
    for c in codes:
        s = stats[c]; n = s["answers"] or 1
        pct = lambda k: f"{s[k] / n:.0%}"
        md.append(f"| {c} {spec['attributes'][c]['name']} | {s['answers']:,} | {pct('docs')} | {pct('in_docs')} | {pct('in_pages')} | {pct('model_pages')} | "
                  f"{pct('proposal')} | **{pct('right')}** | {s['right'] / (s['proposal'] or 1):.0%} | {pct('scale_miss')} |")
    md.append("")
    if a.reader == "model":
        from model_reader import cost
        md += [f"Model calls: {usage['calls']:,} ({usage['model_errors']:,} failed); input tokens {usage['input_tokens']:,}; "
               f"output tokens {usage['output_tokens']:,}; cost about ${cost(usage):.2f} "
               f"(${cost(usage) / (usage['calls'] or 1) * 1000:.2f} per 1,000 answers). "
               f"Proposals dropped because the named page does not print the number: {usage['quote_not_on_page']:,}. "
               f"Replies cut at the token limit: {usage['cut_at_token_limit']:,}. "
               f"The model's pages hold the validated number for {sum(s['model_pages'] for s in stats.values()):,} answers; "
               f"it gave no figure for {sum(s['declined with the number on its pages'] for s in stats.values()):,} of them.", ""]
        md += ["Other years read in the same call (the other year columns of the same figure; one call per answer since 31 Oct 2026). "
               "\"Right\" compares them with the validated answer of the same company and attribute for that year, where there is one:", "",
               "| Attribute | Calls with a figure | Other years returned | Per call | With a validated answer | Right |",
               "| --- | --: | --: | --: | --: | --: |"]
        for c in codes:
            s = stats[c]
            md.append(f"| {c} {spec['attributes'][c]['name']} | {s['proposal']:,} | {s['other years']:,} | {s['other years'] / (s['proposal'] or 1):.1f} | "
                      f"{s['other years validated']:,} | {s['other years right']:,} ({s['other years right'] / (s['other years validated'] or 1):.0%}) |")
        md.append("")
        if declined:
            md += ["No figure although its pages print the validated number (first 20, with the model's reason):", ""] + [f"- {d}" for d in declined] + [""]
    md += ["Pages that print the validated number but were not kept (answers where the number is in the documents and not on the 25 ranked pages):", "",
           "| Attribute | Missed | No label on the page | Label on the next or previous page | Ranked too low | Their best rank (median) | Words printed before the number where no label matched |",
           "| --- | --: | --: | --: | --: | --: | --- |"]
    for c in codes:
        s = stats[c]; rk = sorted(miss_ranks[c])
        n_miss = s["miss: no label on the page"] + s["miss: label on the next or previous page"] + s["miss: ranked too low"]
        top = ", ".join(p for p, k in miss_phrases[c].most_common(40) if k >= 2 and len(p) > 3)[:300]
        md.append(f"| {c} {spec['attributes'][c]['name']} | {n_miss:,} | {s['miss: no label on the page']:,} | "
                  f"{s['miss: label on the next or previous page']:,} | {s['miss: ranked too low']:,} | "
                  f"{rk[len(rk) // 2] if rk else ''} | {top} |")
    md.append("")
    if any(stats[c]["more terms ok"] + stats[c]["more terms timeout"] for c in codes):
        md += ["Second query with the attribute's more_terms (its own time limit, " + MORE_TERMS_TIMEOUT + "): " + "; ".join(
            f"{c} {stats[c]['more terms ok']} answered, {stats[c]['more terms timeout']} timed out" for c in codes
            if stats[c]["more terms ok"] + stats[c]["more terms timeout"]) + ".", ""]
    md += ["The report's own content index (GRI, ESRS, table of contents) as a signal: answers where an index page named a page "
           "for the attribute, and where a named page prints the validated number (the number must also be in the year's documents):", "",
           "| Attribute | Index named a page | Named page prints the number | Of answers with the number in the documents |",
           "| --- | --: | --: | --: |"]
    for c in codes:
        s = stats[c]
        md.append(f"| {c} {spec['attributes'][c]['name']} | {s['index cited']:,} | {s['index hit']:,} | {s['index hit'] / (s['in_docs'] or 1):.0%} |")
    md.append("")
    for c in codes:
        if wrong[c]:
            md += [f"Wrong proposals, {c} (first {len(wrong[c])}):", ""] + [f"- {w}" for w in wrong[c]] + [""]
    open("extract_eval.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    run.finish("ok", rows_in=len(todo), rows_out=sum(s["right"] for s in stats.values()), counters={c: dict(stats[c]) for c in codes})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("extract_eval.md", "w").write("### Extraction: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
