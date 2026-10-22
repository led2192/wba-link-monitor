#!/usr/bin/env python3
"""
doc_scope.py — is this file a whole report, or only a part of one? Decided by rules, no AI field.

Companies often publish a report both whole and in pieces: chapters ("honda-SR-2025-en-005.pdf"),
page ranges ("SCSR25e_p108-162.pdf"), the financial-statements part, an appendix, an executive
summary or a two-page highlights sheet. For anyone extracting figures, the piece and the whole are
different things. Each report-type document gets:

    scope           Full document / Part of a report / Summary or short document
    scope_reason    the rule that decided, in plain words ("page range in the file name: p108-162")

Rules, first match wins (decide()):
  1. the file name says it is a piece: a page range (p108-162, pp12-20, pages_4-9), a chapter or
     section number (chapter3, sec-2, part-1, _005 after a report code), or the words excerpt,
     extract, appendix, exhibit, annex, supplement, addendum                       -> Part of a report
  2. the title says it is a piece: "section", "chapter", "excerpt", "extract", "appendix", "annex",
     "supplement", "additional information", "financial data section", "part 2"   -> Part of a report
  3. the title or file name says summary: "executive summary", "highlights", "at a glance",
     "summary", "fact sheet", "in brief"                                            -> Summary or short document
  4. the cover starts on a printed page number well past the start (page 38) and the file is short
     for that ("starts at page 38, has 12 pages")                                     -> Part of a report
  (a table-of-contents rule was tried and dropped: figures on contents pages read as page numbers)
  5. very short for its type: fewer than 12 pages for an annual, integrated, sustainability or ESG
     report, or a 10-K (a chapter, a press release, a data sheet, a cover)            -> Summary or short document
  6. otherwise                                                                        -> Full document
Rules only use the file name, the title (doc_label), page_count and cover_text, so the same
document always gets the same answer and the reason can be checked by anyone.

Scope: the report families (Annual Report, Integrated Report, Sustainability Report, ESG Report,
CSR Report, 10K Form, Impact Report), by AI type, else keyword type. Queue: rows with scope blank,
plus rows decided before their page count was known; --all redoes every row (after a rule change).

Checked on 21-22 Oct 2026: on 60 random report documents judged by hand, the rules were right on 50
of 54 (the AI field doc_scope 48 of 52, and it leaves non-English documents blank); on the 1,450
files analysts chose for the Source Library, 1,381 come out "Full document" and most of the other
69 are data sheets, supplements and summaries. Dry run by default; --commit writes.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE
"""
import argparse, collections, os, re, sys, time
from urllib.parse import unquote, urlsplit

API = "https://api.airtable.com/v0"
REPORT_TYPES = {"Annual Report", "Integrated Report", "Sustainability Report", "ESG Report", "CSR Report",
                "10K Form", "Impact Report"}
FULL, PART, SUMMARY = "Full document", "Part of a report", "Summary or short document"
SHORT = 12      # fewer pages than this is not a whole annual, integrated or sustainability report

NAME_PART = [
    (re.compile(r"(?<![a-z0-9])(?:p|pp|pg|pages?)[-_. ]?\d{1,3}[-_](?:p|pp)?\d{1,3}(?![0-9])", re.I), "page range in the file name"),
    (re.compile(r"(?<![a-z])(?:chapter|chap|ch|section|sec|part|kapitel|capitulo|capítulo)[-_. ]?\d{1,2}(?!\d)", re.I), "chapter or section number in the file name"),
    (re.compile(r"(?<![a-z])(?:excerpt|extract|appendix|appendices|exhibit|annex|anexo|anhang|supplement|addendum|section|chapter)(?![a-z])", re.I), "the file name says it is a piece"),
    (re.compile(r"[-_](?:en|e|jp|ja|cn|zh)[-_](?!0+(?:[-_.]|$))0\d{1,2}(?:[-_.]|$)", re.I), "chapter number after the language code in the file name"),
    (re.compile(r"(?<![a-z])(?:(?:ar|sr|ir|sir|cr|esg)\d{2,4}e?(?:[-_](?:en|e|j|jp))?[-_](?:0?[1-9]|[1-3]\d)|report[-_]0\d{1,2})(?:en|e)?(?![0-9])(?![-_]\d)", re.I), "chapter number after the report code in the file name"),
    (re.compile(r"^(?:0?[1-9]|[1-3]\d)$|(?:introduction|contents|cover)[-_ ]?\d?$", re.I), "the file name is a chapter number or an opening section"),
    (re.compile(r"(?<![a-z])(?:ceo|chairman|chairmans|president)[-_ ]?(?:letter|message|statement)|"
                r"(?:letter|message)[-_ ]to[-_ ](?:shareholders|stockholders)", re.I), "the file is the leader's letter"),
    (re.compile(r"(?:report|sustainability|integrated|annual)\d{0,4}[-_ ](?:social|environment(?:al)?|governance|risk[-_ ]?m(?:gt|anagement)?|financial[-_ ]section)(?![a-z])", re.I),
     "one topic of a report in the file name"),
    (re.compile(r"(?<![a-z])(?:managements?[-_ ]discussion|md[-&_]?a|governance|remuneration[-_ ]report|auditor'?s?[-_ ]?(?:rep|report)|"
                r"financial[-_ ]statements|notes[-_ ]to|relationship[-_ ]capital|natural[-_ ]capital|human[-_ ]capital|"
                r"kurzfassung|short[-_ ]version|at[-_ ]a?[-_ ]?glance)(?![a-z])", re.I),
     "one section of a report in the file name"),
]
# a file named for the whole report ("integrated-report-and-financial-statements") is not a section
WHOLE_REPORT_NAME = re.compile(r"(?:annual|integrated|sustainability|esg)[-_ ]?report|report[-_ ]and[-_ ]|"
                               r"environmental[-_ ]social|sustainability[-_ ]and[-_ ]governance", re.I)
# "Appendix 4E" is the Australian full-year report form, not an appendix of something else
# "SEC Form 17-A" is the Philippine annual report
NOT_A_PIECE = re.compile(r"appendix[-_ ]?4[de]|sec[-_ ]*(?:form[-_ ]*)?17", re.I)
# a page range at the end of the name ("DBS-AR17-92-96"), but not a date ("2025-03-19", "4-11-24")
# or a fiscal span ("FY-24-25"): the token before it must not be a number
PAGE_PAIR = re.compile(r"(?:^|[-_ ])([^-_ ]*)[-_](\d{1,3})[-_](\d{1,3})$")
TITLE_PART = re.compile(r"(?<![a-z])(?:(?<!pursuant to )section(?!\s*\d)(?!\s+1[35])|chapter|excerpt|extract|appendix(?!\s*4[de])|annex|"
                        r"supplement(?:ary)?\s+(?:information|data)|additional\s+information|financial\s+data\s+section|"
                        r"part\s+(?:[1-9]|i{1,3}|iv|one|two|three)\b)(?![a-z])", re.I)
SUMMARY_WORDS = re.compile(r"(?<![a-z])(?:executive\s+summary|highlights|at\s+a\s+glance|summary\s+(?:report|version)|"
                           r"fact\s*sheet|in\s+brief|overview\s+brochure|summary)(?![a-z])", re.I)
LEAD_PAGE = re.compile(r"^\s*(\d{1,3})\s+[A-Za-z]")
TOC = re.compile(r"\b(?:contents|table of contents|índice|indice|sommaire|inhalt)\b", re.I)


def file_name(url, filename=""):
    name = unquote(urlsplit((url or "").split("?")[0]).path.rstrip("/").rsplit("/", 1)[-1])
    return name if name.lower().endswith(".pdf") or not filename else unquote(filename)


def toc_last_page(cover):
    """Largest page number listed in a table of contents on the first pages, or None."""
    m = TOC.search(cover or "")
    if not m:
        return None
    nums = [int(n) for n in re.findall(r"(?<![\d.,])(\d{1,3})(?![\d.,%])", cover[m.end():m.end() + 2500])]
    nums = [n for n in nums if 2 <= n <= 900]
    # a contents list counts upwards; keep the run of increasing numbers
    run, last = [], 0
    for n in nums:
        if n > last:
            run.append(n); last = n
    return run[-1] if len(run) >= 4 else None


def decide(url, filename, title, pages, cover, doc_type=""):
    """(scope, reason)."""
    pages = int(pages or 0)
    name = file_name(url, filename)
    stem = re.sub(r"\.pdf$", "", name, flags=re.I)
    if not NOT_A_PIECE.search(stem):
        for rx, why in NAME_PART:
            m = rx.search(stem)
            if m and not ("chapter number" in why and pages >= 150) \
                    and not (why.startswith("one section") and WHOLE_REPORT_NAME.search(stem)):
                return PART, f"{why}: {m.group(0).strip('-_. ')}"
    m = PAGE_PAIR.search(stem)
    if (m and not m.group(1).isdigit() and int(m.group(2)) >= 2
            and 2 <= int(m.group(3)) - int(m.group(2)) < 300):
        return PART, f"page range at the end of the file name: {m.group(2)}-{m.group(3)}"
    t = title or ""
    m = TITLE_PART.search(t)
    if m:
        return PART, f"the title says it is a piece: \"{m.group(0)}\""
    m = SUMMARY_WORDS.search(t) or SUMMARY_WORDS.search(stem.replace("-", " ").replace("_", " "))
    if m:
        return SUMMARY, f"the title or file name says summary: \"{m.group(0)}\""
    m = LEAD_PAGE.match(cover or "")
    if m and pages and int(m.group(1)) >= 20 and pages < int(m.group(1)):
        return PART, f"the first page is printed page {m.group(1)}, the file has {pages} pages"
    if pages and pages < SHORT and doc_type in REPORT_TYPES:
        return SUMMARY, f"only {pages} pages: too short to be a whole {doc_type}"
    if not pages:
        return FULL, "no page count yet; no sign of a piece"
    return FULL, f"{pages} pages, no sign of a piece"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    token, base = os.environ.get("AIRTABLE_TOKEN"), os.environ.get("AIRTABLE_BASE")
    if not (token and base):
        sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
    from monitor_core import airtable_request
    H = {"Authorization": f"Bearer {token}"}
    types = ",".join(f"{{source_type_check}}='{t}'" for t in sorted(REPORT_TYPES))
    kw = ",".join(f"AND({{source_type_check}}='', {{source_type}}='{t}')" for t in sorted(REPORT_TYPES))
    # rows decided before their page count was known are looked at again once it is
    queue = ", OR({scope}=BLANK(), AND(LEFT({scope_reason}&'', 16)='no page count ye', {page_count}))"
    formula = f"AND(NOT({{discard}}), OR({types},{kw}){'' if a.all else queue})"
    params = [("pageSize", "100"), ("filterByFormula", formula)] + [("fields[]", f) for f in (
        "library_id", "document_url", "file", "doc_label", "page_count", "cover_text", "source_type_check",
        "source_type", "scope", "scope_reason")]
    url = f"{API}/{base}/report_library"
    counts, reasons, ups, done, offset = collections.Counter(), collections.Counter(), [], 0, None
    sample = []
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", url, H, params=p).json()
        for r in j.get("records", []):
            f = r["fields"]
            att = (f.get("file") or [{}])[0]
            t = (f.get("source_type_check") or "").strip() or (f.get("source_type") or "").strip()
            scope, why = decide(f.get("document_url"), att.get("filename", ""), f.get("doc_label"),
                                f.get("page_count"), f.get("cover_text"), t)
            counts[scope] += 1; reasons[why.split(":")[0].split(",")[0]] += 1
            if (f.get("scope"), f.get("scope_reason")) != (scope, why):
                ups.append({"id": r["id"], "fields": {"scope": scope, "scope_reason": why}})
            if scope != FULL and len(sample) < 40:
                sample.append((f.get("library_id"), scope, why, (f.get("doc_label") or "")[:80]))
            done += 1
        offset = j.get("offset")
        if not offset or (a.limit and done >= a.limit):
            break
        time.sleep(0.21)
    lines = [f"## Document scope ({'written' if a.commit else 'dry run, nothing written'})", "",
             f"Report documents read: {done}. Rows to update: {len(ups)}.", "",
             "Scope: " + ", ".join(f"{k} {v}" for k, v in counts.most_common()), "",
             "Reasons: " + ", ".join(f"{k} {v}" for k, v in reasons.most_common()), "",
             "| library_id | scope | reason | title |", "|---|---|---|---|"]
    lines += [f"| {l} | {s} | {w.replace('|', '/')} | {t.replace('|', '/')} |" for l, s, w, t in sample]
    out = "\n".join(lines)
    print(out, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(out + "\n")
    if not a.commit:
        print("DRY RUN. Re-run with --commit to write.")
        return
    HJ = {**H, "Content-Type": "application/json"}
    for i in range(0, len(ups), 10):
        try:
            airtable_request("PATCH", url, HJ, {"records": ups[i:i + 10], "typecast": True})
        except Exception as e:
            print(f">>> WARN: write failed ({e}); those rows stay queued.", flush=True)
        time.sleep(0.22)
    print(f">>> wrote {len(ups)} rows.")


if __name__ == "__main__":
    main()
