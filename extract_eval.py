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
DB_FAILS_IN_A_ROW = 10                # answers failing on the database one after another: an outage, so the run stops
MORE_TERMS_TIMEOUT = "4s"            # the second query (an attribute's more_terms) may take this long, else it is skipped
MORE_POOL = 30                       # pages it may add (bodies are read for these only)
INDEX_WEIGHT = 1.5                   # page score added to a page the report's own content index cites (content_index.py)
NEIGHBOUR_WEIGHT = 0.5               # page score added for a well-scored page next to this one (neighbours, 1 Nov 2026)
SIBLING_WEIGHT = 1.0                 # page score added to the page where a sibling attribute's figure is (siblings, 2 Nov 2026)
SIBLING_NEAR = {0: 1.0, 1: 0.6, 2: 0.3}   # share of SIBLING_WEIGHT by distance from the sibling's page
NEIGHBOUR_DECAY = {1: 1.0, 2: 0.4}   # share of the neighbour's score that reaches this page, by distance in pages
NEIGHBOUR_TOP = 10                   # the best pages whose missing next and previous pages are fetched and scored
from company_memory import MEMORY_WEIGHT  # page score added to a page carrying the line the company used another year
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


def short_hash(x):
    """A stable 12-character key: the same answer gets the same key in every run, and the key says nothing."""
    import hashlib
    return hashlib.sha1(str(x).encode()).hexdigest()[:12]


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


def page_score(text_rank, cand, body, year, labels, exclude, doc_weight, index_boost=0.0, memory_boost=0.0, sibling_boost=0.0):
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
    return (1.0 * text_rank + 1.0 * min(c, 1) + 0.5 * tab + 0.5 * has_year + 0.5 * hits + 0.7 * doc_weight + INDEX_WEIGHT * index_boost
            + MEMORY_WEIGHT * memory_boost + SIBLING_WEIGHT * sibling_boost)


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


PAGE_TEXT = "db"         # the text the model reads: the database's plain text, or a page re-read with its tables (--page-text)


def read_text(lid, page, body):
    """The text of a model page: the database text, or with --page-text pymupdf|docling the page re-read from
    the PDF with its tables as tables (table_text.py), the database text when that fails."""
    if PAGE_TEXT == "db":
        return body
    import table_text
    md = table_text.page_text(lid, page, PAGE_TEXT)
    return md if md else body


PAGE_ORDER = "today"     # "reversed" (--order): the model reads the same pages in the opposite order
SELECT = "base"          # how the rank half of the model's pages is chosen: "base" (today) or "learned" (--select)
SELECT_MODEL = None      # page_select.load() when SELECT is "learned"
EMBEDDER = None          # page_select.Embedder() when SELECT is "learned"


def model_pages(scored, code=None, attr=None, pages=None, docs=None, fold=None):
    """The MODEL_PAGES pages the model reads: pages_for_model on today's order, or with --select learned on
    the order page_select.py learned from the analysts' cited pages (the candidate half is the same either
    way; only the rank half changes). fold: the company fold of a validated answer (its own fold left out
    of the training), None for an answer nobody has validated."""
    if SELECT == "learned" and SELECT_MODEL and pages:
        import page_select
        o = page_select.order(SELECT_MODEL, code, attr, pages, {d[0]: d[1] for d in docs or []}, fold, EMBEDDER)
        if o:
            seen = set(o)
            scored = [scored[i] for i in o] + [x for j, x in enumerate(scored) if j not in seen]
    return pages_for_model(scored, MODEL_PAGES)


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
FY_TYPES = ["Annual Report", "Integrated Report", "10K Form", "Financial Statement", "Registration Document"]
FY_CACHE = {}


def fy_end_month(cur, wba):
    """The month (1-12) the company's fiscal year ends, or None when its documents do not say. The commonest
    month among its annual-family documents' period_end (documents.period_end, from report_library); when none
    carries one yet, read from the first twelve pages of up to three of those documents with fiscal_year.decide.
    Cached for the run. A year ending January to May is the reporting year before (the pipeline's rule), so
    for such a company reporting year 2024 is the document dated 2025 (2 Nov 2026: ORIX, year end March, had
    its integrated report dated 2024, April 2023 to March 2024, read as 2024's: the "2024" column of the
    statements was the year ended March 31, 2024, which is reporting year 2023)."""
    if wba in FY_CACHE:
        return FY_CACHE[wba]
    month = None
    try:
        cur.execute("select substr(period_end, 6, 2), count(*) from public.documents where wba_id = %s "
                    "and period_end ~ '^[0-9]{4}-[0-9]{2}$' and doc_type = any(%s) group by 1 order by 2 desc, 1 limit 1",
                    (wba, FY_TYPES))
        row = cur.fetchone()
        if row and 1 <= int(row[0]) <= 12:
            month = int(row[0])
    except Exception:
        cur.connection.rollback()
    if month is None:
        try:
            import fiscal_year as FY
            cur.execute("select library_id from public.documents where wba_id = %s and doc_type = any(%s) and year is not null "
                        "order by year desc, library_id limit 3", (wba, FY_TYPES))
            months = collections.Counter()
            for (lid,) in cur.fetchall():
                cur.execute("select page, body from public.pages where library_id = %s and page <= 12 order by page", (lid,))
                _, end, score, *_ = FY.decide(cur.fetchall())
                if end and len(end) == 7 and score >= 45:
                    months[int(end[5:7])] += 1
            if months:
                month = months.most_common(1)[0][0]
        except Exception:
            cur.connection.rollback()
    FY_CACHE[wba] = month
    return month


def document_year(year, fy_month):
    """The documents' year (the year the period ends, the library's convention) for a reporting year: the
    reporting year itself, or the next when the company's fiscal year ends January to May (reporting_year.py)."""
    from reporting_year import shifted
    return year + 1 if shifted(fy_month) else year


def year_documents(cur, wba, year, attr):
    """([(library_id, doc_type, year)], {library_id: weight}): the company's documents of the reporting year
    (document_year: the year after it for a fiscal year ending January to May) or the next, preferred types
    first, at most MAX_DOCS; the weight favours the preferred types and the year's own document."""
    ranks = {t: i for i, t in enumerate(attr.get("doc_types", []))}
    want = document_year(year, fy_end_month(cur, wba))
    cur.execute("select library_id, coalesce(doc_type, ''), coalesce(year, 0) from public.documents "
                "where wba_id = %s and year in (%s, %s)", (wba, want, want + 1))
    docs = sorted(cur.fetchall(), key=lambda d: (ranks.get(d[1], 99), d[2] != want, d[0]))[:MAX_DOCS]
    nt = max(len(ranks), 1)
    weight = {d[0]: (1 - ranks[d[1]] / nt if d[1] in ranks else 0.0) * (1.0 if d[2] == want else 0.7) for d in docs}
    return docs, weight


def neighbour_pages(cur, rows, have, n=NEIGHBOUR_TOP):
    """[(library_id, page, body, 0.0)]: the page before and after each of the n best-scored rows that is not
    in the pool yet. A table often runs on to the next page, and the report's index names the page a
    section starts on while the figure sits on the one after; neither page carries the label, so the word
    index never returns it (1 Nov 2026). rows: [(score, library_id, page, ...)]."""
    want = set()
    for _, lid, page, *_ in sorted(rows, key=lambda r: -r[0])[:n]:
        for q in (page - 1, page + 1):
            if q >= 1 and (lid, q) not in have:
                want.add((lid, q))
    if not want:
        return []
    cur.execute("select library_id, page, body from public.pages where library_id = any(%s) and page = any(%s)",
                (sorted({l for l, _ in want}), sorted({p for _, p in want})))
    return [(lid, page, body, 0.0) for lid, page, body in cur.fetchall() if (lid, page) in want]


def with_neighbours(rows, weight=None, decay=None):
    """The rows with NEIGHBOUR_WEIGHT x the best neighbour's share added: a page next to a well-scored page
    of the same document scores higher, less so two pages away (what lies at page x is more like what lies
    at x+1 than at x+30). Shares are of the best score in the pool, from the scores before this step, so
    nothing travels further than two pages. rows: [(score, library_id, page, body, top)]."""
    weight = NEIGHBOUR_WEIGHT if weight is None else weight
    decay = decay or NEIGHBOUR_DECAY
    if not rows or not weight:
        return rows
    top = max(r[0] for r in rows)
    if top <= 0:
        return rows
    base = {(r[1], r[2]): r[0] for r in rows}
    out = []
    for r in rows:
        best = 0.0
        for d, share in decay.items():
            for q in (r[2] - d, r[2] + d):
                sc = base.get((r[1], q))
                if sc:
                    best = max(best, sc / top * share)
        out.append((r[0] + weight * best,) + tuple(r[1:]))
    return out


SIBLING_CACHE = {}
SIBLINGS_ON = True


def sibling_pages(cur, wba, year, attr, spec):
    """{(library_id, page): share} for the attribute's siblings (attributes.json "siblings"): the best page the
    rule reader finds for each sibling's figure, that page 1.0, the next and previous 0.6, two away 0.3. A
    sibling is read first and once per company and year (cached). 2 Nov 2026, measured on 331 company-years
    with validated answers: Scope 1 and Scope 2 are printed on the same page 89 to 93% of the time, revenue and
    low-carbon capex 46% (70% within two pages), while Scope 2 and capex are the attributes whose page the
    reader misses most."""
    out = {}
    for code in attr.get("siblings") or []:
        sib = spec["attributes"].get(code)
        if not sib:
            continue
        key = (wba, year, code)
        if key not in SIBLING_CACHE:
            best = None
            docs, weight = year_documents(cur, wba, year, sib)
            if docs:
                rows = ranked_pages(cur, [d[0] for d in docs], weight, year, sib, spec)
                with_cand = [r for r in rows if r[4]]
                if with_cand:
                    top = max(with_cand, key=lambda r: r[0])
                    best = (top[1], top[2])
            SIBLING_CACHE[key] = best
        best = SIBLING_CACHE[key]
        if best:
            lid, page = best
            for dist, share in SIBLING_NEAR.items():
                for p in {page - dist, page + dist}:
                    if p >= 1:
                        out[(lid, p)] = max(out.get((lid, p), 0.0), share)
    return out


BM25_POOL = 0             # --bm25-pool N: also the N best pages of the documents by BM25 over any label word (7 Nov 2026)
BM25_CANDIDATES = 300     # pages holding any label word that BM25 scores (their bodies are read)
TEXT_RANK = "ts"          # the text score in page_score: "ts" (ts_rank_cd) or "bm25" (--text-rank, 4 Nov 2026)


def bm25_text_rank(cur, found, attr):
    """found with its text score replaced by BM25 over the attribute's label words, scaled 0 to 1 by the best
    page (page_ranker.py: 63% of cited pages in the top 3 against 45% for ts_rank_cd in the ranking
    experiment). Same pool of pages; only the text signal of page_score changes."""
    import collections
    import rank_eval as R
    from page_ranker import RANKER
    terms = list(dict.fromkeys(t for l in attr.get("labels", []) + attr.get("more_terms", []) for t in R.query_terms(l)))
    if not terms or not found:
        return found
    RANKER._ensure(cur, terms)
    scores = []
    for lid, page, body, _ in found:
        tf = collections.Counter(R.tokens(body))
        scores.append(R.bm25(tf, len(tf), RANKER.avg_len, terms, RANKER.idfs))
    top = max(scores) or 1.0
    return [(lid, page, body, s / top) for (lid, page, body, _), s in zip(found, scores)]


def ranked_pages(cur, ids, weight, year, attr, spec, info=None, memory=None, siblings=None):
    """[(score, library_id, page, body, best rule candidate)] for the label-matching pages of the documents,
    plus the pages the documents' own content index cites for the attribute (content_index.py, 31 Oct 2026),
    which get INDEX_WEIGHT and join the pool even when no label is on them, plus (memory, from
    company_memory.memory_for, 1 Nov 2026) the pages carrying the line the company printed the figure on
    another year, which get MEMORY_WEIGHT, plus (siblings, from sibling_pages, 2 Nov 2026) the pages where a
    sibling attribute's figure is and the ones next to it, which get SIBLING_WEIGHT times their share. info, a
    dict when given, receives "cited": {(library_id, page): weight} and "siblings": the sibling pages."""
    import content_index, company_memory
    cited = content_index.cited(cur, ids, attr)
    if info is not None:
        info["cited"] = dict(cited)
    cur.execute("select library_id, page, body, ts_rank_cd(words, websearch_to_tsquery('simple', %s), 32) as rk from public.pages "
                "where library_id = any(%s) and words @@ websearch_to_tsquery('simple', %s) "
                "order by rk desc limit %s",
                (label_query(attr["labels"]), ids, label_query(attr["labels"]), POOL))
    found = cur.fetchall()
    found += more_terms_pages(cur, ids, attr, {(lid, page) for lid, page, _, _ in found}, info)
    siblings = {k: v for k, v in (siblings or {}).items() if k[0] in set(ids)}
    if info is not None:
        info["siblings"] = dict(siblings)
    missing = (set(cited) | set(siblings)) - {(lid, page) for lid, page, _, _ in found}
    if missing:
        cur.execute("select library_id, page, body from public.pages where library_id = any(%s) and page = any(%s)",
                    (sorted({l for l, _ in missing}), sorted({p for _, p in missing})))
        found += [(lid, page, body, 0.0) for lid, page, body in cur.fetchall() if (lid, page) in missing]
    if memory:
        found += company_memory.memory_pages(cur, ids, memory, {(lid, page) for lid, page, _, _ in found})
    if BM25_POOL:
        added = bm25_pool_pages(cur, ids, attr, {(lid, page) for lid, page, _, _ in found}, BM25_POOL,
                                max((rk for *_, rk in found), default=0.1))
        if info is not None:
            info["bm25_added"] = {(lid, page) for lid, page, _, _ in added}
        found += added
    if TEXT_RANK == "bm25":
        found = bm25_text_rank(cur, found, attr)
    def score(lid, page, body, rk):
        cs = candidates(body, year, attr, spec)
        top = max(cs, key=lambda c: c[0]) if cs else None
        sc = page_score(rk, top[0] if top else None, body, year, attr.get("labels", []) + attr.get("more_terms", []),
                        attr.get("exclude", []), weight.get(lid, 0), cited.get((lid, page), 0.0), company_memory.boost(body, memory),
                        siblings.get((lid, page), 0.0))
        return (sc, lid, page, body, top)
    rows = [score(lid, page, body, rk) for lid, page, body, rk in found]
    # the pages next to the best ones join the pool (a table running on, the page after an index entry)
    rows += [score(lid, page, body, rk) for lid, page, body, rk in
             neighbour_pages(cur, rows, {(lid, page) for lid, page, _, _ in found})]
    if info is not None:
        info["neighbours"] = {(lid, page) for _, lid, page, _, _ in rows[len(found):]}
    return with_neighbours(rows)


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


def bm25_pool_pages(cur, ids, attr, have, k, top_rank):
    """[(library_id, page, body, rank)] of the k best pages of the documents by BM25 over the attribute's label
    words (any of them, not every word of a label), that the label query did not find. The ranking experiment
    (rank_eval.py, 7 Nov 2026, 2,206 cited pages) puts the cited page in the top 3 for 63% of answers with
    BM25 against 44% with ts_rank_cd, and the oracle run says finding pages costs most for capex, employees
    and revenue; replacing the text score with BM25 inside the label pool lost pages (5 Nov 2026), so this
    only ADDS pages. Their rank is BM25 scaled to the pool's best ts rank. Own timeout and savepoint, as
    more_terms_pages."""
    import collections
    import rank_eval as R
    from page_ranker import RANKER
    terms = list(dict.fromkeys(t for l in attr.get("labels", []) + attr.get("more_terms", []) for t in R.query_terms(l)))
    if not terms or k <= 0:
        return []
    q = " | ".join(terms)
    cur.execute("show statement_timeout")
    before = cur.fetchone()[0]
    try:
        with cur.connection.transaction():
            cur.execute(f"set local statement_timeout = '{MORE_TERMS_TIMEOUT}'")
            cur.execute("with p as materialized (select library_id, page, words from public.pages where library_id = any(%s)), "
                        "m as (select library_id, page, ts_rank_cd(words, to_tsquery('simple', %s), 32) as rk from p "
                        "      where words @@ to_tsquery('simple', %s) order by rk desc limit %s) "
                        "select m.library_id, m.page, b.body from m join public.pages b using (library_id, page)",
                        (ids, q, q, BM25_CANDIDATES))
            rows = [r for r in cur.fetchall() if (r[0], r[1]) not in have]
        RANKER._ensure(cur, terms)
    except Exception:
        return []
    finally:
        try:
            cur.execute(f"set statement_timeout = '{before}'")
        except Exception:
            pass
    scored = []
    for lid, page, body in rows:
        tf = collections.Counter(R.tokens(body or ""))
        scored.append((R.bm25(tf, len(tf), RANKER.avg_len, terms, RANKER.idfs), lid, page, body))
    scored.sort(key=lambda x: -x[0])
    best = scored[0][0] if scored and scored[0][0] > 0 else 1.0
    return [(lid, page, body, (top_rank or 0.1) * sc / best) for sc, lid, page, body in scored[:k] if sc > 0]


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


def read_values(cur, wba, year, attr, spec, usage, known=None):
    """Every figure the model reads for a company and attribute from the documents of one year, with no
    validated answer needed: the same documents, ranking and pages as evaluate(). The year asked comes first
    when the pages print it; the other entries are the other years the same pages print (one call for all,
    31 Oct 2026). Each entry: value, printed, scale, library_id, page, unit, year, column, and "asked" (the
    year whose documents were read: when it differs from "year", the figure is a comparison column of a
    later report). [] when nothing was read. known: {year: right value} for this company and attribute
    (validated or accepted); the nearest other year's becomes the company's memory (company_memory.py)."""
    from model_reader import read_years
    from company_memory import memory_for, hint
    docs, weight = year_documents(cur, wba, year, attr)
    if not docs:
        return []
    memory = memory_for(cur, wba, attr, year, known, year_documents) if known else None
    if memory:
        usage["with a memory from another year"] += 1
    pages = keep_pages(ranked_pages(cur, [d[0] for d in docs], weight, year, attr, spec, memory=memory,
                                    siblings=sibling_pages(cur, wba, year, attr, spec)))
    scored = [((top[0] if top else -99) - 0.05 * i, top, lid, page, body) for i, (_, lid, page, body, top) in enumerate(pages)]
    code = next((c for c, v in spec["attributes"].items() if v is attr), None)
    best = model_pages(scored, code, attr, pages, docs)
    if not best:
        return []
    got = read_years({"wba": wba, "year": year, "hint": hint(memory), "fy_month": fy_end_month(cur, wba)}, attr,
                     [(lid, page, body) for _, _, lid, page, body in best], usage)
    for e in got:
        e["asked"] = year
    return got


def read_value(cur, wba, year, attr, spec, usage, known=None):
    """What the model reader proposes for a company, attribute and year: the entry of read_values for that
    year (the others under "others"), or None."""
    got = read_values(cur, wba, year, attr, spec, usage, known)
    mine = next((e for e in got if e["year"] == year), None)
    if mine:
        mine["others"] = [e for e in got if e is not mine]
    return mine


def oracle_pages(holding, rows, best_pages, n=MODEL_PAGES):
    """The reader's ceiling: the pages that print the analyst's figure first, in their ranked order (pages the
    ranking never reached last), then the model's usual pages, MODEL_PAGES in all. Never an option: it uses the
    answer to choose the pages."""
    pos = {(x[1], x[2]): i for i, x in enumerate(rows)}
    lead = [(0.0, None, lid, page, body) for lid, page, body in sorted(holding, key=lambda h: pos.get((h[0], h[1]), 10 ** 6))][:n]
    have = {(x[2], x[3]) for x in lead}
    return lead + [x for x in best_pages if (x[2], x[3]) not in have][:n - len(lead)]


def evaluate(cur, g, attr, spec, reader="rules", usage=None, known=None):
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
    from company_memory import memory_for, hint
    memory = memory_for(cur, g["wba"], attr, g["year"], known, year_documents) if known else None
    r["memory"] = bool(memory)
    r["fy_shift"] = document_year(g["year"], fy_end_month(cur, g["wba"])) != g["year"]   # a January-to-June year end
    rows = ranked_pages(cur, ids, weight, g["year"], attr, spec, info, memory,
                        siblings=sibling_pages(cur, g["wba"], g["year"], attr, spec) if SIBLINGS_ON else None)
    pages = keep_pages(rows)
    r["in_pages"] = any(found_on(p[3], v) for p in pages for v, _, _ in forms)
    r["more_terms"] = info.get("more_terms")
    r["index_cited"] = bool(info.get("cited"))                       # the report's own index named a page
    r["index_hit"] = any((lid, page) in info.get("cited", {}) for lid, page, _ in holding)
    r["sibling_pages"] = bool(info.get("siblings"))
    r["sibling_hit"] = any(info.get("siblings", {}).get((lid, page), 0) >= 0.6 for lid, page, _ in holding)   # the number within one page of the sibling's
    r["sibling_kept"] = r["sibling_hit"] and r["in_pages"]
    r["neighbour_pages"] = len(info.get("neighbours", ()))
    r["neighbour_hit"] = any((lid, page) in info.get("neighbours", ()) for lid, page, _ in holding)   # the number sits on a page brought in as a neighbour
    r["neighbour_kept"] = any((lid, page) in info.get("neighbours", ()) and any(found_on(body, v) for v, _, _ in forms) for _, lid, page, body, _ in pages)
    if r["in_docs"] and not r["in_pages"]:
        r["miss"] = retrieval_miss(holding, rows, forms)
    # each kept page's best rule candidate, used by the rule reader and to pick the model's pages
    scored = [((top[0] if top else -99) - 0.05 * i, top, lid, page, body) for i, (_, lid, page, body, top) in enumerate(pages)]
    import rank_eval
    best_pages = model_pages(scored, g["attribute"], attr, pages, docs, rank_eval.fold_of(g["wba"]))
    if SELECT == "oracle" and holding:       # stage diagnostic, not an option (7 Nov 2026)
        best_pages = oracle_pages(holding, rows, best_pages)
    if PAGE_ORDER == "reversed":              # metamorphic check: the same pages in the opposite order
        best_pages = list(reversed(best_pages))
    r["model_pages_have_number"] = any(found_on(b, v) for *_, b in best_pages for v, _, _ in forms)
    if reader == "model":
        import model_reader
        read_years = model_reader.read_candidates if model_reader.SELECTOR == "candidates" else model_reader.read_years
        note = {}
        all_got = read_years(dict(g, hint=hint(memory), fy_month=fy_end_month(cur, g["wba"])), attr,
                             [(lid, page, read_text(lid, page, body)) for _, _, lid, page, body in best_pages], usage, note=note)
        got = next((e for e in all_got if e["year"] == g["year"]), None)
        r["others"] = [e for e in all_got if e is not got]        # the other years the same call read
        r["dropped"] = list(note.get("dropped") or [])      # why the checks dropped entries (7 Nov 2026)
        r["declined_why"] = note.get("why") or ("(only other years: " + ", ".join(str(e["year"]) for e in all_got) + ")" if all_got and not got else None)
        if "candidates" in note:                            # --selector candidates (8 Nov 2026)
            r["cands"] = len(note["candidates"])
            r["cand_right"] = next((i for i, v in enumerate(note["candidates"]) if right(v, g["value"])), None)
            r["choice"] = note.get("choice")
        r["right_other_year"] = next((e["year"] - g["year"] for e in all_got if e is not got and right(e["value"], g["value"])), None)
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
    ap.add_argument("--memory", default="on", choices=["on", "off"], help="use the company's right answer for another year (company_memory.py)")
    ap.add_argument("--text-rank", default="ts", choices=["ts", "bm25"], help="the text score of page ranking: ts_rank_cd or BM25 (page_ranker.py)")
    ap.add_argument("--siblings", default="on", choices=["on", "off"], help="boost the pages of a sibling attribute's figure (attributes.json siblings)")
    ap.add_argument("--guidance", default="today", choices=["today", "learned", "check", "md"], help="learned: the choice rules of decision_rules.json in the model's prompt, and figures on an avoided line dropped (model_reader.RULES); check: only the drop, today's prompt; md: WBA's own assessment guidance per attribute (Pipeline QA attribute_guidance) in the prompt")
    ap.add_argument("--page-text", default="db", choices=["db", "pymupdf", "docling"], help="the text the model reads: the database text, or each page re-read from the PDF with its tables kept as tables (table_text.py)")
    ap.add_argument("--select", default="base", choices=["base", "learned", "oracle"], help="how the rank half of the model's 8 pages is chosen: today's order or the learned one (page_select.py); oracle: the pages that print the analyst's figure first (a stage diagnostic: the reader's ceiling, never an option)")
    ap.add_argument("--period-check", default="all", choices=["all", "currency-only"], help="drop an entry whose period ends in another month than the company's financial year: for every attribute (today) or for money only")
    ap.add_argument("--bm25-pool", type=int, default=0, help="also add the N best pages of the documents by BM25 over any label word to the ranking pool (0: off, today)")
    ap.add_argument("--unstated-basis", default="keep", choices=["default-only", "keep"], help="a Scope 2 figure whose basis the page does not state: filed under market-based too (the default since 7 Nov 2026) or under location-based only (default-only, the earlier rule)")
    ap.add_argument("--selector", default="off", choices=["off", "candidates"], help="candidates: a generous extractor proposes up to 3 figures with row, column and footnote, and a second call chooses one against the attribute's definition and notes (model_reader.read_candidates)")
    ap.add_argument("--keys", default="", help="only these answers: WBA|GMC|YEAR, comma separated (8 Nov 2026: to compare with another team's pilot on the same answers)")
    ap.add_argument("--order", default="today", choices=["today", "reversed"], help="the order of the model's pages; reversed is a metamorphic check of position bias")
    a = ap.parse_args()
    a.memory = a.memory == "on"
    global SIBLINGS_ON, TEXT_RANK
    SIBLINGS_ON = a.siblings == "on"
    TEXT_RANK = a.text_rank
    global SELECT, SELECT_MODEL, EMBEDDER, PAGE_TEXT
    SELECT = a.select
    global PAGE_ORDER
    PAGE_ORDER = a.order
    import model_reader
    model_reader.UNSTATED_BASIS = a.unstated_basis
    model_reader.PERIOD_CHECK = a.period_check
    model_reader.SELECTOR = a.selector
    global BM25_POOL
    BM25_POOL = a.bm25_pool
    PAGE_TEXT = a.page_text
    if a.guidance == "md":
        import model_reader
        from narrative_eval import read_base
        rows = read_base(os.environ.get("AIRTABLE_QA_TOKEN", "").strip(), os.environ.get("AIRTABLE_QA_BASE", "appJQ2mRtjHjzLq9E").strip(),
                         "attribute_guidance", ["gmc", "guidance", "md_prompt"])
        model_reader.MD_GUIDANCE = {r["fields"].get("gmc"): r["fields"] for r in rows if r["fields"].get("gmc")}
        print(f">>> WBA guidance read for {len(model_reader.MD_GUIDANCE)} attributes", flush=True)
        if not model_reader.MD_GUIDANCE:
            open("extract_eval.md", "w").write("### Extraction: --guidance md found no rows in Pipeline QA attribute_guidance; nothing run.\n")
            return 1
    if a.guidance in ("learned", "check"):
        import model_reader
        model_reader.RULES_IN_PROMPT = a.guidance == "learned"
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "decision_rules.json")
        if not os.path.exists(p):
            open("extract_eval.md", "w").write("### Extraction: --guidance learned needs decision_rules.json (decision_mining.py --save); nothing run.\n")
            return 1
        model_reader.RULES = json.load(open(p, encoding="utf-8"))
    if SELECT == "learned":
        import page_select
        SELECT_MODEL = page_select.load()
        if not SELECT_MODEL:
            open("extract_eval.md", "w").write(f"### Extraction: --select learned needs {os.path.basename(page_select.MODEL_FILE)} (select_eval.py --save); nothing run.\n")
            return 1
        EMBEDDER = page_select.Embedder()   # part of what was measured: no silent fallback
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
    if a.keys:                                   # only these answers, e.g. another pilot's company-attribute-years
        want = {tuple(k.split("|")) for k in a.keys.split(",") if k.count("|") == 2}
        answers = [g for g in answers if (g["wba"], g["attribute"], str(g["year"])) in want]
        print(f">>> --keys: {len(want):,} asked, {len(answers):,} with a validated answer", flush=True)
    by = collections.defaultdict(list)
    for g in answers:
        by[g["attribute"]].append(g)
    queues = [by[c][:limit] if limit else by[c] for c in codes]
    todo = [q[i] for i in range(max((len(q) for q in queues), default=0)) for q in queues if i < len(q)]
    print(f">>> {len(todo):,} answers to read ({', '.join(f'{c} {len(q)}' for c, q in zip(codes, queues))})", flush=True)

    stats = {c: collections.Counter() for c in codes}
    declined = []
    drop_reasons = collections.defaultdict(collections.Counter)   # answers declined although the pages print the figure, by why
    miss_ranks = collections.defaultdict(list)
    miss_phrases = {c: collections.Counter() for c in codes}
    wrong = collections.defaultdict(list)
    t0, cut, db_stop = time.time(), False, ""
    failed_in_a_row, first_errors = 0, []
    outcomes = []                                  # per answer, for paired comparisons between runs (compare_runs.py)
    with __import__("evidence_db").connect() as conn:
        cur = conn.cursor()
        cur.execute("set statement_timeout = '30s'")
        for i, g in enumerate(todo, 1):
            if time.time() - t0 > a.budget * 60:
                cut = True; break
            if i % 100 == 0:
                print(f"  {i:,}/{len(todo):,} in {(time.time() - t0) / 60:.0f} min", flush=True)
            s = stats[g["attribute"]]; s["answers"] += 1
            try:
                known = {y: v for (w, c, y), v in gold_index.items() if w == g["wba"] and c == g["attribute"] and y != g["year"]}
                r = evaluate(cur, g, spec["attributes"][g["attribute"]], spec, a.reader, usage, known if a.memory else None)
            except Exception as e:
                if type(e).__name__ == "ModelRefused":           # the API said no: stop, keep what was read and say why
                    s["answers"] -= 1                            # (8 Nov 2026: a run lost 200 read answers to a credit stop)
                    db_stop = f"the model API refused the call ({str(e)[:160]})"
                    break
                if len(first_errors) < 3:
                    first_errors.append(f"{type(e).__name__}: {str(e).splitlines()[0][:160] if str(e) else ''}")
                    print(f"  database error on {g['id']}: {first_errors[-1]}", flush=True)
                s["database did not answer"] += 1
                failed_in_a_row += 1
                # 4 Nov 2026: with the evidence database overloaded by a night of parallel runs, two full runs
                # spent 100 minutes on 23 answers that all failed, then reported "ok". Ten failures in a row is
                # an outage, not bad luck: stop, say so, and keep nothing.
                if failed_in_a_row >= DB_FAILS_IN_A_ROW:
                    db_stop = f"the evidence database did not answer {failed_in_a_row} times in a row ({first_errors[0]})"
                    break
                try:
                    conn.rollback(); cur.execute("set statement_timeout = '30s'")
                except Exception:
                    db_stop = f"the evidence database connection was lost ({first_errors[0]})"
                    break
                continue
            failed_in_a_row = 0
            outcomes.append([short_hash(g["id"]), short_hash(g["wba"]), g["attribute"], int(bool(r["right"])),
                             int(bool(r["in_docs"])), int(bool(r["in_pages"])), int(bool(r["proposal"]))])
            s["right in_docs"] += bool(r["right"] and r["in_docs"])
            if "cands" in r:
                s["sel answers"] += 1
                s["sel a right candidate"] += r["cand_right"] is not None
                s["sel right candidate first"] += r["cand_right"] == 0
                s["sel rescued"] += bool(r["right"] and r.get("choice"))          # chose candidate 2 or 3, and right
                s["sel chose a wrong one over a right one"] += bool(r["cand_right"] is not None and not r["right"] and r.get("choice") is not None)
                s["sel chose none over a right one"] += bool(r["cand_right"] is not None and r.get("choice") is None)
            for k in ("docs", "in_docs", "in_pages", "proposal", "right", "scale_miss"):
                s[k] += bool(r[k])
            s["model_pages"] += bool(r.get("model_pages_have_number"))
            s["index cited"] += bool(r.get("index_cited")); s["index hit"] += bool(r.get("index_hit"))
            s["sibling answers"] += bool(r.get("sibling_pages")); s["sibling hit"] += bool(r.get("sibling_hit")); s["sibling kept"] += bool(r.get("sibling_kept"))
            s["neighbour pages"] += r.get("neighbour_pages", 0); s["neighbour hit"] += bool(r.get("neighbour_hit")); s["neighbour kept"] += bool(r.get("neighbour_kept"))
            if not r["right"] and r.get("right_other_year") is not None:
                s[f"right for the year {r['right_other_year']:+d}"] += 1
            if r.get("fy_shift"):
                s["fy shift"] += 1; s["fy shift right"] += bool(r["right"]); s["fy shift proposal"] += bool(r["proposal"])
            if r.get("memory"):
                s["memory"] += 1; s["memory in_pages"] += bool(r["in_pages"]); s["memory right"] += bool(r["right"])
            else:
                s["no memory"] += 1; s["no memory in_pages"] += bool(r["in_pages"]); s["no memory right"] += bool(r["right"])
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
                for d in sorted(set(r.get("dropped") or [])) or ["the model gave no figure for the year"]:
                    drop_reasons[g["attribute"]][d] += 1
                if len(declined) < 20:
                    declined.append(f"{g['id']}: \"{r.get('declined_why') or 'no reason given'}\"")
            if r["proposal"] and not r["right"] and len(wrong[g["attribute"]]) < 12:
                (v, raw, sc, lab), lid, page = r["got"]
                wrong[g["attribute"]].append(f"{g['id']}: validated {g['value']:,.2f} {g['unit']}; read {raw:,.2f} × {sc:g} "
                                             f"after \"{lab}\" ({lid} p.{page})")

    title = "model reader (" + os.environ.get("READER_MODEL", "claude-haiku-5-5") + ")" if a.reader == "model" else "generic reader (no model)"
    if SELECT == "oracle":
        title += ", ORACLE pages (the pages printing the analyst's figure first: the reader's ceiling, not an option)"
    elif SELECT != "base":
        title += f", pages chosen by {SELECT}"
    if PAGE_ORDER != "today":
        title += f", pages in {PAGE_ORDER} order"
    if PAGE_TEXT != "db":
        title += f", pages re-read with their tables by {PAGE_TEXT}"
    if a.guidance != "today":
        title += (", with WBA's assessment guidance" if a.guidance == "md" else
                  ", with the choice rules learned from the analysts" + (" (as a check only)" if a.guidance == "check" else ""))
    md = [f"### The {title} against the validated answers" + ("" if a.commit else " (dry run, sample)"), "",
          f"{sum(s['answers'] for s in stats.values()):,} answers with a value" + (f" ({limit} per attribute)" if limit else "")
          + ("; stopped at the time budget" if cut else "") + (f"; STOPPED: {db_stop}" if db_stop else "") + ". Shares of those answers; the reader never sees the answer.", "",
          "| Attribute | Answers | Document for the year | Number in those documents | Number on the ranked pages | Number on the model's pages | Proposal | Right | Right among proposals | Right where the number is in the documents | Right digits, wrong scale |",
          "| --- | --: | --: | --: | --: | --: | --: | --: | --: | --: | --: |"]
    for c in codes:
        s = stats[c]; n = s["answers"] or 1
        pct = lambda k: f"{s[k] / n:.0%}"
        md.append(f"| {c} {spec['attributes'][c]['name']} | {s['answers']:,} | {pct('docs')} | {pct('in_docs')} | {pct('in_pages')} | {pct('model_pages')} | "
                  f"{pct('proposal')} | **{pct('right')}** | {s['right'] / (s['proposal'] or 1):.0%} | {s['right in_docs'] / (s['in_docs'] or 1):.0%} | {pct('scale_miss')} |")
    md.append("")
    if a.reader == "model":
        from model_reader import cost
        md += [f"Model calls: {usage['calls']:,} ({usage['model_errors']:,} failed); input tokens {usage['input_tokens']:,}; "
               f"output tokens {usage['output_tokens']:,}; cost about ${cost(usage):.2f} "
               f"(${cost(usage) / (usage['calls'] or 1) * 1000:.2f} per 1,000 answers). "
               f"Proposals dropped because the named page does not print the number: {usage['quote_not_on_page']:,}. "
               f"Replies cut at the token limit: {usage['cut_at_token_limit']:,}. "
               f"The model's pages hold the validated number for {sum(s['model_pages'] for s in stats.values()):,} answers; "
               f"it gave no figure for {sum(s['declined with the number on its pages'] for s in stats.values()):,} of them."
               + (f" Figures dropped by the learned rules (on a line with an avoided word): {usage['dropped by the learned rules']:,}." if a.guidance != "today" else ""), ""]
        other = {c: {k: v for k, v in st.items() if k.startswith("right for the year ")} for c, st in stats.items()}
        if any(other.values()):
            md += ["Wrong for the year asked but the model's figure for another year it read equals the analyst's: "
                   + "; ".join(f"{c} " + ", ".join(f"{k[len('right for the year '):]}: {v}" for k, v in sorted(o.items()))
                               for c, o in other.items() if o) + ".", ""]
        if PAGE_TEXT != "db":
            import table_text
            st = table_text.STATS
            md += [f"Pages re-read with {PAGE_TEXT}: {st.get('pages parsed', 0):,} parsed ({st.get('tables', 0):,} tables, "
                   f"{st.get('seconds', 0) / max(st.get('pages parsed', 0), 1):.1f} s a page); kept the database text for "
                   + ", ".join(f"{v:,} {k}" for k, v in sorted(st.items()) if k not in ("pages parsed", "tables", "seconds")) + "."
                   + "".join(f" First {p} error: {e}" for p, e in table_text.ERRORS.items()), ""]
        md += ["Other years read in the same call (the other year columns of the same figure; one call per answer since 31 Oct 2026). "
               "\"Right\" compares them with the validated answer of the same company and attribute for that year, where there is one:", "",
               "| Attribute | Calls with a figure | Other years returned | Per call | With a validated answer | Right |",
               "| --- | --: | --: | --: | --: | --: |"]
        for c in codes:
            s = stats[c]
            md.append(f"| {c} {spec['attributes'][c]['name']} | {s['proposal']:,} | {s['other years']:,} | {s['other years'] / (s['proposal'] or 1):.1f} | "
                      f"{s['other years validated']:,} | {s['other years right']:,} ({s['other years right'] / (s['other years validated'] or 1):.0%}) |")
        md.append("")
        if any(stats[c]["sel answers"] for c in codes):
            md += ["Extractor and selector (--selector candidates): how often one of the up to 3 candidates is the validated "
                   "figure (the extractor's recall), how often it came first, how often the selector picked candidate 2 or 3 and "
                   "was right (rescued), and how often a right candidate was there but the selector chose another or none.", "",
                   "| Attribute | Answers | A right candidate | Right one first | Right | Rescued | Chose another over the right one | Chose none over the right one |",
                   "| --- | --: | --: | --: | --: | --: | --: | --: |"]
            for c in codes:
                s = stats[c]
                n = s["sel answers"]
                if n:
                    md.append(f"| {c} {spec['attributes'][c]['name']} | {n:,} | {s['sel a right candidate'] / n:.0%} | {s['sel right candidate first'] / n:.0%} | "
                              f"{s['right'] / (s['answers'] or 1):.0%} | {s['sel rescued']:,} | {s['sel chose a wrong one over a right one']:,} | "
                              f"{s['sel chose none over a right one']:,} |")
            md.append("")
        if declined:
            md += ["No figure although its pages print the validated number (first 20, with the model's reason):", ""] + [f"- {d}" for d in declined] + [""]
        if drop_reasons:
            md += ["Why: answers with no figure although the pages print the validated number, by the reason our checks dropped "
                   "the model's entries (an answer can have several; \"the model gave no figure for the year\" when nothing was dropped):", "",
                   "| Attribute | Reason | Answers |", "| --- | --- | --: |"]
            md += [f"| {c} | {why} | {n} |" for c in codes for why, n in drop_reasons.get(c, collections.Counter()).most_common()] + [""]
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
    md += ["Neighbours (1 Nov 2026): the page before and after each of the 10 best-scored pages joins the pool, and a page next to a "
           "well-scored page scores higher. Answers where the validated number sits on a page that came in only as a neighbour, "
           "and where that page was kept among the 25:", "",
           "| Attribute | Neighbour pages added per answer | Number on a neighbour page | Kept among the ranked pages |",
           "| --- | --: | --: | --: |"]
    for c in codes:
        s = stats[c]
        md.append(f"| {c} {spec['attributes'][c]['name']} | {s['neighbour pages'] / (s['answers'] or 1):.1f} | {s['neighbour hit']:,} | {s['neighbour kept']:,} |")
    md.append("")
    md += ["Siblings (2 Nov 2026): the best page of a sibling attribute's figure (Scope 1 for Scope 2, revenue for low-carbon capex) "
           "and the pages next to it join the pool and score higher" + ("" if SIBLINGS_ON else " (switched off in this run)")
           + ". Answers with a sibling page, answers whose validated number is on it or one page away, and of those, kept among the ranked pages:", "",
           "| Attribute | With a sibling page | Number on or next to it | Kept |", "| --- | --: | --: | --: |"]
    for c in codes:
        s = stats[c]
        if spec["attributes"][c].get("siblings"):
            md.append(f"| {c} {spec['attributes'][c]['name']} | {s['sibling answers']:,} | {s['sibling hit']:,} | {s['sibling kept']:,} |")
    md.append("")
    md += ["The company's memory (company_memory.py): answers where the validated value for another year was found in that "
           "year's documents, so the line the company printed it on was searched for and boosted this year"
           + ("" if a.memory else " (switched off in this run)") + ". Number on the ranked pages and right, with and without a memory:", "",
           "| Attribute | With a memory | On the ranked pages | Right | Without | On the ranked pages | Right |",
           "| --- | --: | --: | --: | --: | --: | --: |"]
    for c in codes:
        s = stats[c]
        md.append(f"| {c} {spec['attributes'][c]['name']} | {s['memory']:,} | {s['memory in_pages'] / (s['memory'] or 1):.0%} | "
                  f"{s['memory right'] / (s['memory'] or 1):.0%} | {s['no memory']:,} | {s['no memory in_pages'] / (s['no memory'] or 1):.0%} | "
                  f"{s['no memory right'] / (s['no memory'] or 1):.0%} |")
    md.append("")
    md += ["Fiscal years ending January to May (2 Nov 2026): the reporting year's document is the one dated the year after, "
           "and the model is told the year end. Answers of such companies, proposals and right:", "",
           "| Attribute | Answers with a January-to-May year end | Proposal | Right |", "| --- | --: | --: | --: |"]
    for c in codes:
        s = stats[c]
        md.append(f"| {c} {spec['attributes'][c]['name']} | {s['fy shift']:,} | {s['fy shift proposal'] / (s['fy shift'] or 1):.0%} | "
                  f"{s['fy shift right'] / (s['fy shift'] or 1):.0%} |")
    md.append("")
    for c in codes:
        if wrong[c]:
            md += [f"Wrong proposals, {c} (first {len(wrong[c])}):", ""] + [f"- {w}" for w in wrong[c]] + [""]
    open("extract_eval.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    # per-answer outcomes for compare_runs.py (paired McNemar and a company bootstrap between two runs). Hashed keys,
    # right or wrong only: no value, no company id, no analyst answer.
    print(">>> OUTCOMES_JSON_BEGIN\n" + json.dumps({"select": SELECT, "order": PAGE_ORDER, "guidance": a.guidance, "page_text": PAGE_TEXT, "unstated_basis": a.unstated_basis, "period_check": a.period_check, "bm25_pool": a.bm25_pool, "selector": a.selector,
                                                    "reader": a.reader, "rows": outcomes}, separators=(",", ":")) + "\n>>> OUTCOMES_JSON_END", flush=True)
    for c in codes:                               # the scoreboard (Pipeline QA evals), one row per attribute
        s = stats[c]; n = s["answers"] - s["database did not answer"]
        if n > 0 and not db_stop:                 # a run cut by an outage is not a score
            run.evaluation(f"reader {c} {spec['attributes'][c]['name']}", n, s["right"] / n, version=f"{a.reader} reader" + "".join(f", {k} {v}" for k, v in (("select", SELECT), ("order", PAGE_ORDER), ("guidance", a.guidance), ("page text", PAGE_TEXT), ("unstated basis", a.unstated_basis), ("period check", a.period_check), ("bm25 pool", a.bm25_pool), ("selector", a.selector))
                                                                              if v not in ("base", "today", "db", "keep", "all", 0, "off")),
                           high_stakes=s["in_pages"] / n, suite="validated answers",
                           per_class={"proposal": round(s["proposal"] / n, 4), "number in the documents": round(s["in_docs"] / n, 4),
                                      "number on the ranked pages": round(s["in_pages"] / n, 4)},
                           notes="accuracy: the proposal matches the validated answer. high_stakes_accuracy: the validated "
                                 "number is on the ranked pages (finding, before reading).")
    if db_stop:
        what = "model_refused" if db_stop.startswith("the model API") else "database_unavailable"
        run.failure("RUN_FAILED", what, key=f"{run.workflow}|{what}", stage="reading", message=db_stop[:500])
    run.finish("failed" if db_stop else "ok", rows_in=len(todo), rows_out=sum(s["right"] for s in stats.values()),
               counters={c: dict(stats[c]) for c in codes})
    return 1 if db_stop else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("extract_eval.md", "w").write("### Extraction: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
