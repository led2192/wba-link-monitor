#!/usr/bin/env python3
"""
type_covers.py: the document type of the documents nobody has typed, read by a model from the first pages.

Why (10 Nov 2026): the Airtable AI field that types documents runs on English documents only, and the address
and cover-title rules leave about 45,000 live documents with no type at all: 28,500 in other languages with a
cover text (Japanese 14,000, Spanish 6,800, French 6,200, German 3,700, Korean 3,100...), 1,700 in English the AI
field has not reached, and 14,600 with no cover text (no file or no text). An untyped document is invisible to
the coverage numbers, to the readers' document choice (spec doc_types) and to the archive rules; on 9 Nov 64 %
of the companies had one. Haiku reads any of these languages, so this script gives those documents the type
the AI field would have given an English one, from the same text: the first 2,500 characters of cover_text.

What it writes: source_type_check (the effective type, the field people correct) on live rows where it is
empty, never where a person or the AI field has set it. The reply "UNKNOWN" is written too, so the row leaves
the queue (UNKNOWN is a choice of the field). The address rule's source_type stays as it is: the disagreement
queue compares the two.

Modes: dry run by default (prints the distribution and 40 examples, writes nothing); --commit writes, 100
documents at a time, so that a budget stop or a refused call loses nothing already read; --check N reads N
documents the AI field has already typed and reports the agreement by type (the measure of this reader, run
before the first --commit); --languages other (default) skips English documents, which the AI field types,
--languages all includes them; --limit, --max-usd (the estimated cost at the Haiku list price).
Env: AIRTABLE_TOKEN, AIRTABLE_BASE, ANTHROPIC_API_KEY (READER_MODEL to use another model); AIRTABLE_QA_TOKEN
for the run record (QA_COMMIT=true writes it).
"""
import argparse, collections, json, os, re, sys, time

API = "https://api.airtable.com/v0"
COVER_CHARS = 2500
WRITE_BATCH = 10
WRITE_PAUSE = 0.22
UNKNOWN = "UNKNOWN"
TYPES = {
    "Annual Report": "the company's yearly report to shareholders, with the business review and usually the accounts",
    "Integrated Report": "a yearly report that presents financial and sustainability performance together, often naming the Integrated Reporting Framework",
    "Sustainability Report": "a standalone yearly report on sustainability, environmental and social performance",
    "ESG Report": "a standalone yearly report named ESG (environmental, social, governance), often with data tables and indices",
    "CSR Report": "a standalone yearly report named corporate social responsibility or corporate citizenship",
    "Impact Report": "a standalone report on the company's impact (social, community, foundation, purpose)",
    "Climate Report": "a report on climate: TCFD, climate transition plan, net zero, carbon, decarbonisation",
    "Environmental Reports": "a report on the environment other than climate: water, biodiversity, waste, environmental data, EMAS",
    "Social Reports": "a report on people: human capital, diversity, gender pay gap, modern slavery statement, labour, health and safety",
    "CDP Report": "a CDP questionnaire response (climate, water, forests)",
    "GRI": "a GRI content index or GRI-referenced sustainability data book",
    "SASB Index": "a SASB or ISSB standards index or table",
    "Engagement Report": "a report on stakeholder, community or investor engagement",
    "Financial Statement": "financial statements and notes, consolidated accounts, auditor's report, without the yearly business review",
    "Interim Reports": "a quarterly or half-year report, results release or interim statement",
    "10K Form": "a US SEC annual filing (Form 10-K, 20-F, 40-F) or proxy statement",
    "Registration Document": "a universal registration document or document de reference filed with a securities regulator",
    "Statutory Documents": "articles of association, bylaws, certificate of incorporation, charters of board committees, shareholder meeting notices and resolutions, prospectuses",
    "Company Presentations": "slides: investor, results, strategy, capital markets day or ESG presentations",
    "Policy Documents": "a policy, standard or statement on one topic (environment, anti-corruption, supplier, tax, privacy...), other than human rights or the code of conduct",
    "Human Rights Policy": "a human rights policy or statement",
    "Code of Conduct": "a code of conduct, code of ethics or business principles, for employees or for suppliers",
    "Certifications": "a certificate or attestation: ISO, management system, product, award, verification statement",
}
SYSTEM = ("You classify company documents into one document type from a fixed list, reading the text of their first "
          "pages in any language. Reply with JSON only.")


def prompt(cover):
    lines = ["Which type of document is this? The types:"]
    lines += [f"- {t}: {d}" for t, d in TYPES.items()]
    lines += [f"- {UNKNOWN}: none of the above (a web page, a brochure, a form, a press release, a product sheet, a notice, a prospectus supplement)",
              "",
              "Rules: the type is what the document is as a whole, not a topic it mentions. A yearly report with a sustainability "
              "chapter is an Annual Report (Integrated Report only when it calls itself integrated). A report named sustainability, "
              "ESG or CSR takes that name. A document covering a quarter or half year is Interim Reports whatever it is called. "
              "Slides are Company Presentations whatever their subject. Text in Japanese, Chinese, Korean, Spanish, French, German "
              "or any other language is typed by the same rules.",
              "", "First pages:", "<cover>", (cover or "")[:COVER_CHARS], "</cover>", "",
              'Reply with JSON: {"type": "<one type exactly as listed>", "confidence": "high" or "low", '
              '"title": "<the document title as printed, in its language, up to 120 characters, or empty>"}']
    return "\n".join(lines)


def parse_reply(text):
    """(type, confidence, title) or (None, None, None) when the reply is not usable. The type must be a listed choice."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None, None, None
    try:
        j = json.loads(m.group(0))
    except ValueError:
        return None, None, None
    t = str(j.get("type") or "").strip()
    if t not in TYPES and t != UNKNOWN:
        low = {k.lower(): k for k in list(TYPES) + [UNKNOWN]}
        t = low.get(t.lower())
    if not t:
        return None, None, None
    conf = "low" if str(j.get("confidence") or "").strip().lower() == "low" else "high"
    return t, conf, str(j.get("title") or "").strip()[:120]


def read_type(cover, usage, call=None):
    """One call: (type, confidence, title). None type when the model gave no usable reply."""
    import model_reader
    call = call or model_reader.call
    text = call([{"role": "user", "content": prompt(cover)}], usage, system=SYSTEM, max_tokens=120)
    return parse_reply(text)


def queue_formula(languages="other", check=False):
    """The rows to read. check: rows the AI field or a person already typed, to measure against. Otherwise live
    rows with a cover text and no type at all (no AI type, no address or cover-rule type); with languages
    "other" the English rows are left to the AI field."""
    parts = ["NOT({discard})", "{cover_text}!=''"]
    if check:
        parts.append("{source_type_check}!=''")
    else:
        parts += ["{source_type_check}=''", "{source_type}=''"]
        if languages == "other":
            parts.append("{doc_language}!='English'")
    return "AND(" + ", ".join(parts) + ")"


def rows(token, base, formula, limit=0, fields=("library_id", "cover_text", "doc_language", "source_type_check", "source_type")):
    """Stream the queue 100 rows at a time (cover_text is up to 10,000 characters)."""
    from monitor_core import airtable_request
    H = {"Authorization": f"Bearer {token}"}
    params = [("pageSize", "100"), ("filterByFormula", formula)] + [("fields[]", f) for f in fields]
    offset, n = None, 0
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", f"{API}/{base}/report_library", H, params=p).json()
        for r in j.get("records", []):
            yield r
            n += 1
            if limit and n >= limit:
                return
        offset = j.get("offset")
        if not offset:
            return
        time.sleep(0.21)


def payloads(results):
    """PATCH bodies for the rows typed: source_type_check set (typecast lets Airtable match the choice by name; no link
    field is touched). results: [(record id, type)]."""
    for i in range(0, len(results), WRITE_BATCH):
        yield {"records": [{"id": rid, "fields": {"source_type_check": t}} for rid, t in results[i:i + WRITE_BATCH]], "typecast": True}


def write(token, base, results):
    from monitor_core import airtable_request
    H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    done = 0
    for body in payloads(results):
        airtable_request("PATCH", f"{API}/{base}/report_library", H, body)
        done += len(body["records"])
        time.sleep(WRITE_PAUSE)
    return done


def agreement(pairs):
    """pairs: [(their type, ours)]. Returns (share agreeing, {their type: (n, agree)}, Counter of (theirs, ours) disagreements)."""
    by = collections.defaultdict(lambda: [0, 0])
    dis = collections.Counter()
    for theirs, ours in pairs:
        by[theirs][0] += 1
        if theirs == ours:
            by[theirs][1] += 1
        else:
            dis[(theirs, ours)] += 1
    n = sum(v[0] for v in by.values())
    return (sum(v[1] for v in by.values()) / n if n else 0.0), dict(by), dis


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="at most this many documents (0 = the whole queue)")
    ap.add_argument("--languages", choices=["other", "all"], default="other", help="other: not the English rows (the AI field types those)")
    ap.add_argument("--check", type=int, default=0, help="read N documents already typed and report the agreement; writes nothing")
    ap.add_argument("--max-usd", type=float, default=5.0, help="stop when the estimated cost reaches this (what was read is written)")
    ap.add_argument("--out", default="type_covers")
    a = ap.parse_args()
    token, base = os.environ.get("AIRTABLE_TOKEN"), os.environ.get("AIRTABLE_BASE")
    if not (token and base):
        sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip() and not os.environ.get("GEMINI_API_KEY", "").strip():
        sys.exit("Set ANTHROPIC_API_KEY (or GEMINI_API_KEY with READER_MODEL=gemini-...).")
    import qa, model_reader
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("type_covers.py")
    t0 = time.time()
    usage = collections.Counter()
    formula = queue_formula(a.languages, check=bool(a.check))
    limit = a.check or a.limit
    typed, pairs, examples, stopped, written, by_lang = collections.Counter(), [], [], None, 0, collections.Counter()
    pending = []                                             # (record id, type) read and not yet written
    try:
        for r in rows(token, base, formula, limit):
            f = r.get("fields", {})
            t, conf, title = read_type(f.get("cover_text") or "", usage)
            if t is None:
                usage["unusable replies"] += 1
                continue
            typed[t] += 1
            by_lang[f.get("doc_language") or "(no language)"] += 1
            if len(examples) < 40:
                examples.append((f.get("library_id"), f.get("doc_language") or "", t, conf, title))
            if a.check:
                pairs.append(((f.get("source_type_check") or "").strip(), t))
            else:                                            # low confidence is written too: a type beats none, and people correct the field
                pending.append((r["id"], t))
            if a.commit and len(pending) >= 100:
                written += write(token, base, pending); pending = []
                print(f"  {written:,} rows written, ${model_reader.cost(usage):.2f}", flush=True)
            if model_reader.cost(usage) >= a.max_usd:
                stopped = f"stopped at the cost cap of ${a.max_usd:.2f}"
                break
    except Exception as e:
        if type(e).__name__ == "ModelRefused":
            stopped = f"stopped: the API refused a call ({str(e)[:160]})"
        else:
            raise
    finally:
        if a.commit and pending:
            written += write(token, base, pending); pending = []
    n = sum(typed.values())
    md = [f"# Document types from the first pages ({'check' if a.check else 'written' if a.commit else 'dry run'})", "",
          f"{n:,} documents read in {time.time() - t0:.0f}s; {usage['calls']:,} calls, {usage['input_tokens']:,} in / {usage['output_tokens']:,} out tokens, "
          f"${model_reader.cost(usage):.2f}{model_reader.cost_note()}; {usage['unusable replies']:,} unusable replies."
          + (f" {stopped}." if stopped else ""), ""]
    if a.check:
        share, by, dis = agreement(pairs)
        md += [f"Agreement with the type already set: {share:.0%} of {len(pairs):,}.", "", "| Their type | Documents | Agree |", "| --- | --: | --: |"]
        md += [f"| {t} | {v[0]:,} | {v[1] / v[0]:.0%} |" for t, v in sorted(by.items(), key=lambda x: -x[1][0])]
        md += ["", "Commonest disagreements (theirs, ours): " + ", ".join(f"{a_} > {b} {c}" for (a_, b), c in dis.most_common(12)), ""]
    else:
        md += [("Written: " if a.commit else "Would write: ") + f"{written if a.commit else len(pending):,} rows (source_type_check).", "",
               "| Type | Documents |", "| --- | --: |"] + [f"| {t} | {c:,} |" for t, c in typed.most_common()]
        md += ["", "| Language | Documents |", "| --- | --: |"] + [f"| {l} | {c:,} |" for l, c in by_lang.most_common(15)]
    md += ["", "Examples (library_id, language, type, confidence, title):", ""] + [f"- {lid} | {lang} | {t} | {conf} | {title}" for lid, lang, t, conf, title in examples]
    out = "\n".join(md) + "\n"
    open(a.out + ".md", "w", encoding="utf-8").write(out)
    print(out, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(out + "\n")
    status = "ok" if not stopped or stopped.startswith("stopped at the cost cap") else "partial"
    run.finish(status, rows_in=n, rows_out=written, counters={"read": n, "written": written, "calls": usage["calls"], "usd": round(model_reader.cost(usage), 3),
                                                             "unusable": usage["unusable replies"], "check": a.check},
               notes=(stopped or ("check" if a.check else "dry run" if not a.commit else "written")))
    return 1 if stopped and "refused" in stopped else 0


if __name__ == "__main__":
    sys.exit(main())
