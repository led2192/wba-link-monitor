#!/usr/bin/env python3
"""
anchor_eval.py: does the text of the link a document was found under tell its type and year better than
the address does?

Proposal 6 of the information retrieval review (4 Nov 2026). On a company's reports page the link usually
says what the file is ("Annual Report 2024", "Sustainability Report 2023 (PDF, 12 MB)") while the address is
often a number (/media/download/3162, /getattachment/8f2c.../). The monitors keep that link text since June
2026, but only in monitored_links.new_links ("<address>  [<link text>]"), for links that appeared after a
page's first visit, and since 4 Nov 2026 for every link in monitored_links.link_texts (and report_library.link_text).

This script measures, before anything is built:
  coverage   how many library documents have link text, and how many of those name a type or a year;
  type       against rows a person verified (verified ticked), how often the link text's type is right, next
             to the address's type (source_type) and the pipeline's type before review (robot_type);
  year       against verified rows, how often the link text's year equals the year (next to robot_year), and how often the link
             text has a year where the year came only from the weakest rule (year_source 7, title or address).
Writes anchor_eval.md and anchor_eval.csv (every document with link text: the three types, both years).
Read-only. No model calls. Env: AIRTABLE_TOKEN, AIRTABLE_BASE; AIRTABLE_QA_TOKEN for the run record.
Options: --companies N (0 = all), to sample on a pull request.
"""
import argparse, collections, csv, os, re, sys, time, traceback
from urllib.parse import quote

import qa
from classify_library import OWNERS, SOURCE_TYPE_TOKENS
from monitor_core import airtable_request, normalize

API = "https://api.airtable.com/v0"
ENTRY = re.compile(r"(\S+)  \[([^\]]*)\]")
YEAR = re.compile(r"(?<!\d)(20[0-3]\d)(?:\s*[/-]\s*(\d{2,4}))?(?!\d)")
# Documents the monitors found on a company page. origin is blank on rows harvested before the field
# existed (first run, 4 Nov 2026: origin = 'discovery' matched no row at all), so blank counts too.
FOUND_ON_PAGES = "OR({origin}='', {origin}='discovery')"
GENERIC = re.compile(r"^(download|pdf|here|click here|read more|view|open|link|report|document|more|\W*)$", re.I)


def text_type(text):
    """The document type a piece of text names, by the same whole-token rules as the address classifier
    (classify_library.classify), or '' when it names none."""
    t = " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()) + " "
    best, best_score = "", 0.0
    for typ, toks in SOURCE_TYPE_TOKENS.items():
        score = 0.0
        for tok in toks:
            if f" {tok} " in t:
                score += len(tok.split()) - (0.1 if len(OWNERS.get(tok, ())) > 1 else 0)
        if score > best_score:
            best, best_score = typ, score
    return best


def text_year(text, this_year=None):
    """The reporting year a link text names: the latest plausible year; for a split year ("2024/25",
    "2024-2025") the first one, the year most of the period falls in. '' when none."""
    import datetime as _dt
    this_year = this_year or _dt.date.today().year
    ys = []
    for m in YEAR.finditer(text or ""):
        y = int(m.group(1))
        if 2000 <= y <= this_year + 1:
            ys.append(y)
    return str(max(ys)) if ys else ""


def parse_new_links(text):
    """{address key: link text} from a monitored page's new_links (newest first, so the first text wins)."""
    out = {}
    for line in (text or "").splitlines():
        body = line.split(": ", 1)[1] if ": " in line[:14] else line
        for part in body.split(" ; "):
            m = ENTRY.match(part.strip())
            if m and m.group(1) not in out and m.group(2).strip() and not GENERIC.match(m.group(2).strip()):
                out[m.group(1)] = m.group(2).strip()
    return out


def fetch(H, base, table, fields, formula=None):
    params = [("pageSize", "100")] + [("fields[]", f) for f in fields] + ([("filterByFormula", formula)] if formula else [])
    out, offset = [], None
    while True:
        p = params + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", f"{API}/{base}/{quote(table)}", H, params=p).json()
        out += j.get("records", [])
        offset = j.get("offset")
        if not offset:
            return out
        time.sleep(0.2)


def pct(a, b):
    return f"{a / b:.0%}" if b else "n/a"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--companies", type=int, default=0, help="companies to sample (0 = all)")
    a = ap.parse_args()
    base, H = os.environ["AIRTABLE_BASE"], {"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}"}
    run = qa.Run("anchor_eval.py")
    from monitor_core import parse_link_texts
    pages = fetch(H, base, "monitored_links", ["wba_id", "new_links", "link_texts"], "OR({new_links}!='', {link_texts}!='')")
    anchors = collections.defaultdict(dict)                    # wba_id -> {key: text}
    for r in pages:
        f = r["fields"]
        # link_texts (every link, since 4 Nov 2026) first, then the texts kept in new_links before that
        for k, t in list(parse_link_texts(f.get("link_texts")).items()) + list(parse_new_links(f.get("new_links")).items()):
            if not GENERIC.match(t):
                anchors[f.get("wba_id") or ""].setdefault(k, t)
    companies = sorted(w for w in anchors if w)
    if a.companies:
        companies = companies[:a.companies]
    print(f">>> {len(pages):,} monitored pages with new links; {sum(len(anchors[w]) for w in companies):,} link texts "
          f"for {len(companies):,} companies", flush=True)
    fields = ["wba_id", "company_name", "document_url", "source_type", "source_type_check", "effective_type", "year",
              "year_source", "verified", "discard", "robot_type", "robot_year"]
    lib = []
    if a.companies:
        for i in range(0, len(companies), 25):
            ors = ",".join(f"{{wba_id}}='{w}'" for w in companies[i:i + 25])
            lib += fetch(H, base, "report_library", fields, f"AND({FOUND_ON_PAGES}, NOT({{discard}}), OR({ors}))")
    else:
        lib = fetch(H, base, "report_library", fields, f"AND({FOUND_ON_PAGES}, NOT({{discard}}))")
    keep = set(companies)
    rows, c = [], collections.Counter()
    for r in lib:
        f = r["fields"]
        if f.get("wba_id") not in keep:
            continue
        c["library documents"] += 1
        text = anchors[f["wba_id"]].get(normalize(f.get("document_url") or ""))
        if not text:
            continue
        c["with link text"] += 1
        at, ay = text_type(text), text_year(text)
        c["link text names a type"] += bool(at)
        c["link text names a year"] += bool(ay)
        rows.append({"wba_id": f["wba_id"], "company": f.get("company_name", ""), "link_text": text,
                     "type_link_text": at, "type_address": f.get("source_type") or "", "type_pipeline": f.get("robot_type") or "",
                     "type_effective": f.get("effective_type") or "", "year_link_text": ay, "year": f.get("year") or "",
                     "year_pipeline": f.get("robot_year") or "", "year_source": f.get("year_source") or "", "verified": "yes" if f.get("verified") else "",
                     "document_url": f.get("document_url") or ""})
    ver = [r for r in rows if r["verified"]]
    def type_acc(key):
        named = [r for r in ver if r[key] and r[key] != "UNKNOWN"]
        right = sum(1 for r in named if r[key] == r["type_effective"])
        return len(named), right
    ty = {k: type_acc(k) for k in ("type_link_text", "type_address", "type_pipeline")}
    unresolved = [r for r in rows if not r["type_address"]]
    fills = sum(1 for r in unresolved if r["type_link_text"])
    yv = [r for r in ver if r["year_link_text"] and r["year"]]
    year_right = sum(1 for r in yv if str(r["year"]) == r["year_link_text"])
    yp = [r for r in yv if r["year_pipeline"]]
    pipe_right = sum(1 for r in yp if str(r["year"]) == str(r["year_pipeline"]))
    weak = [r for r in rows if str(r["year_source"]).startswith("7") or r["year_source"] in ("none", "")]
    weak_with = [r for r in weak if r["year_link_text"]]
    weak_differs = [r for r in weak_with if str(r["year"]) != r["year_link_text"]]
    with open("anchor_eval.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["wba_id"])
        w.writeheader(); w.writerows(rows)
    md = ["### Link text as a type and year signal", "",
          f"{len(companies):,} companies; {c['library documents']:,} documents found by discovery; "
          f"{c['with link text']:,} with link text ({pct(c['with link text'], c['library documents'])}); of those, "
          f"{c['link text names a type']:,} name a type and {c['link text names a year']:,} a year.", "",
          f"Type against the {len(ver):,} verified documents with link text (share right among those where the source names a type):", "",
          "| Source | Names a type | Right |", "| --- | --: | --: |"]
    for k, label in (("type_link_text", "Link text"), ("type_address", "Address (source_type)"),
                     ("type_pipeline", "Pipeline before review (robot_type: the AI's type, else the address's)")):
        n, right = ty[k]
        md.append(f"| {label} | {n:,} | {right:,} ({pct(right, n)}) |")
    md += ["", f"Documents the address could not type: {len(unresolved):,}; the link text names a type for {fills:,} of them "
           f"({pct(fills, len(unresolved))}).", "",
           f"Year: on {len(yv):,} verified documents where the link text names a year, it equals the year on "
           f"{year_right:,} ({pct(year_right, len(yv))}); the pipeline's own year before review (robot_year) was right on "
           f"{pipe_right:,} of {len(yp):,} ({pct(pipe_right, len(yp))}). A June fiscal year makes \"Annual Report 2024\" "
           f"the 2023 reporting year, so some differences are the convention, not a mistake. Of {len(weak):,} documents whose year came only from the weakest "
           f"rule (title or address) or from nothing, {len(weak_with):,} have a year in the link text, and it differs on "
           f"{len(weak_differs):,}.", "",
           "The rows are in anchor_eval.csv. Nothing was changed."]
    open("anchor_eval.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    run.finish("ok", rows_in=c["library documents"], rows_out=len(rows), counters=dict(c))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("anchor_eval.md", "w").write("### Link text evaluation: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
