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
from value_eval import CURRENCY_SCALES, EMISSION_SCALES, found_on, printed_forms, tsquery_for

SPEC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "attributes.json")
MAX_DOCS, MAX_PAGES = 4, 25
MODEL_PAGES = 3                     # pages given to the model reader
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
        for m in re.finditer(r"(?<![a-z])" + re.escape(lab.lower()) + r"(?![a-z])", t):
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


def label_query(labels):
    """websearch query: any of the labels, each as a phrase."""
    ascii_labels = [l for l in labels if re.fullmatch(r"[\w\s\-']+", l, flags=re.ASCII)]
    return " or ".join(f'"{l}"' if " " in l else l for l in ascii_labels) or "zzzz"


# ----------------------------------------------------------------------------------------------
def evaluate(cur, g, attr, spec, reader="rules", usage=None):
    """Stage results for one answer. reader "rules": the best-scored labelled row on the ranked pages;
    "model": a language model reads the MODEL_PAGES pages whose rule candidates scored best."""
    r = {"docs": False, "in_docs": False, "in_pages": False, "proposal": False, "right": False, "scale_miss": False}
    ranks = {t: i for i, t in enumerate(attr.get("doc_types", []))}
    cur.execute("select library_id, coalesce(doc_type, ''), coalesce(year, 0) from public.documents "
                "where wba_id = %s and year in (%s, %s)", (g["wba"], g["year"], g["year"] + 1))
    docs = sorted(cur.fetchall(), key=lambda d: (ranks.get(d[1], 99), d[2] != g["year"], d[0]))[:MAX_DOCS]
    if not docs:
        return r
    r["docs"] = True
    ids = [d[0] for d in docs]
    order = {lid: i for i, lid in enumerate(ids)}
    scales = EMISSION_SCALES if attr.get("unit_family") == "tco2e" else CURRENCY_SCALES
    forms = printed_forms(g["value"], scales)
    if forms:
        q = " | ".join(f"({tsquery_for(v, d)})" for v, _, d in forms)
        cur.execute("select body from public.pages where library_id = any(%s) and words @@ to_tsquery('simple', %s) limit 40", (ids, q))
        r["in_docs"] = any(found_on(b, v) for (b,) in cur.fetchall() for v, _, _ in forms)
    cur.execute("select library_id, page, body, ts_rank(words, websearch_to_tsquery('simple', %s)) as rk from public.pages "
                "where library_id = any(%s) and words @@ websearch_to_tsquery('simple', %s)",
                (label_query(attr["labels"]), ids, label_query(attr["labels"])))
    pages = sorted(cur.fetchall(), key=lambda p: (order[p[0]], -p[3]))[:MAX_PAGES]
    r["in_pages"] = any(found_on(p[2], v) for p in pages for v, _, _ in forms)
    # each page's best rule candidate, used by the rule reader and to pick the model's pages
    scored = []
    for i, (lid, page, body, _) in enumerate(pages):
        cs = candidates(body, g["year"], attr, spec)
        top = max(cs, key=lambda c: c[0]) if cs else None
        scored.append(((top[0] if top else -99) - 0.05 * i, top, lid, page, body))
    if reader == "model":
        from model_reader import read_with_model
        best_pages = sorted(scored, key=lambda x: -x[0])[:MODEL_PAGES]
        got = read_with_model(g, attr, [(lid, page, body) for _, _, lid, page, body in best_pages], usage)
        r["model_pages_have_number"] = any(found_on(b, v) for *_, b in best_pages for v, _, _ in forms)
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
    by = collections.defaultdict(list)
    for g in answers:
        by[g["attribute"]].append(g)
    queues = [by[c][:limit] if limit else by[c] for c in codes]
    todo = [q[i] for i in range(max((len(q) for q in queues), default=0)) for q in queues if i < len(q)]
    print(f">>> {len(todo):,} answers to read ({', '.join(f'{c} {len(q)}' for c, q in zip(codes, queues))})", flush=True)

    stats = {c: collections.Counter() for c in codes}
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
            if r["proposal"] and not r["right"] and len(wrong[g["attribute"]]) < 12:
                (v, raw, sc, lab), lid, page = r["got"]
                wrong[g["attribute"]].append(f"{g['id']}: validated {g['value']:,.2f} {g['unit']}; read {raw:,.2f} × {sc:g} "
                                             f"after \"{lab}\" ({lid} p.{page})")

    title = "model reader (" + os.environ.get("READER_MODEL", "claude-haiku-5-5") + ")" if a.reader == "model" else "generic reader (no model)"
    md = [f"### The {title} against the validated answers" + ("" if a.commit else " (dry run, sample)"), "",
          f"{sum(s['answers'] for s in stats.values()):,} answers with a value" + (f" ({limit} per attribute)" if limit else "")
          + ("; stopped at the time budget" if cut else "") + ". Shares of those answers; the reader never sees the answer.", "",
          "| Attribute | Answers | Document for the year | Number in those documents | Number on the ranked pages | Proposal | Right | Right among proposals | Right digits, wrong scale |",
          "| --- | --: | --: | --: | --: | --: | --: | --: | --: |"]
    for c in codes:
        s = stats[c]; n = s["answers"] or 1
        pct = lambda k: f"{s[k] / n:.0%}"
        md.append(f"| {c} {spec['attributes'][c]['name']} | {s['answers']:,} | {pct('docs')} | {pct('in_docs')} | {pct('in_pages')} | "
                  f"{pct('proposal')} | **{pct('right')}** | {s['right'] / (s['proposal'] or 1):.0%} | {pct('scale_miss')} |")
    md.append("")
    if a.reader == "model":
        from model_reader import cost
        md += [f"Model calls: {usage['calls']:,} ({usage['model_errors']:,} failed); input tokens {usage['input_tokens']:,}; "
               f"output tokens {usage['output_tokens']:,}; cost about ${cost(usage):.2f} "
               f"(${cost(usage) / (usage['calls'] or 1) * 1000:.2f} per 1,000 answers). "
               f"Proposals dropped because the named page does not print the number: {usage['quote_not_on_page']:,}. "
               f"The model's pages hold the validated number for {sum(s['model_pages'] for s in stats.values()):,} answers.", ""]
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
