#!/usr/bin/env python3
"""
classify_library.py — Level 1 classification of report_library, from the URL only, no download.

Matches the WBA Source Type tokens (the curated multilingual set from tiered-discovery's
verify_types, plus a few strong dictionary phrases) against the full URL path (folders + file
name) of each PDF, and writes the best-scoring Source Type back to report_library, with a
confidence. It resolves about two thirds of the corpus for free; the opaque-URL remainder is left
unlabelled for a Level 2 pass that reads the document content.

Writes, per row:
  source_type        one of the 25 WBA Source Types, or empty when nothing matched
  match_confidence   "high" (a specific multi-word term matched), "low" (only a generic word),
                     or "unresolved" (no token in the URL -> needs content reading)

By default only rows with an empty source_type are processed (so the nightly run only touches the
new PDFs the harvester just added). CLASSIFY_FORCE=true (or --force) re-processes everything.

Modes:
  --source csv --csv <file with a document_url column>   classify offline, print the distribution
  --source airtable                                      read report_library (default); dry-run
  --commit                                               write source_type / match_confidence back

Env (airtable): AIRTABLE_TOKEN, AIRTABLE_BASE, AIRTABLE_LIBRARY_TABLE (default report_library)
Fields to add to report_library: source_type (Single select), match_confidence (Single line text)
"""
import argparse, os, re, sys, time, collections
from urllib.parse import quote, urlsplit

API = "https://api.airtable.com/v0"
from monitor_core import airtable_request
F_ID = "library_id"; F_URL = "document_url"; F_TYPE = "source_type"; F_CONF = "match_confidence"

# Curated, overlapping on purpose (an ESG doc that is really a sustainability report still counts).
# Generic single words are kept only where they are still discriminating for that type.
import json as _json
_TERMS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "source_type_terms.json")
try:
    SOURCE_TYPE_TOKENS = _json.load(open(_TERMS_PATH, encoding="utf-8"))
except Exception:
    SOURCE_TYPE_TOKENS = {"Sustainability Report": ["sustainability report", "sustainab"],
                          "Annual Report": ["annual report"], "Policy Documents": ["policy"]}


def classify(document_url):
    """Return (source_type, confidence). Whole-token match against the whole URL path,
    so short tokens like 'gri' or 'iso' do not match inside 'nigeria' or 'vision'."""
    path = urlsplit(document_url.split("?", 1)[0]).path.lower()
    text = " " + re.sub(r"\s+", " ",
                        path.replace("-", " ").replace("_", " ").replace("/", " ").replace(".", " ")) + " "
    best_t, best_score, best_specific = None, 0, False
    for t, toks in SOURCE_TYPE_TOKENS.items():
        score = 0; specific = False
        for tok in toks:
            if f" {tok} " in text:
                score += len(tok.split())
                if len(tok.split()) >= 2:
                    specific = True
        if score > best_score:
            best_t, best_score, best_specific = t, score, specific
    if not best_t:
        return "", "unresolved"
    return best_t, ("high" if best_specific else "low")


def from_csv(path):
    import csv
    csv.field_size_limit(10_000_000)
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return [r for r in csv.DictReader(fh)]


def from_airtable(base, token, table, force):
    import requests
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}"}
    params = [("pageSize", "100"), ("fields[]", F_ID), ("fields[]", F_URL), ("fields[]", F_CONF)]
    out = []; offset = None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        r = airtable_request("GET", url, headers, params=p); r.raise_for_status()
        j = r.json()
        for rec in j.get("records", []):
            f = rec.get("fields", {})
            # processed rows carry a match_confidence (incl. "unresolved"); skip them unless forced
            if force or not f.get(F_CONF):
                out.append((rec["id"], f.get(F_URL, "")))
        offset = j.get("offset"); time.sleep(0.22)
        if not offset: break
    return out


# ---- Level 1b: label from the downloaded file -------------------------------------------------
# Many sites serve PDFs from addresses with no words in them (AmRest /en/media/download/3162,
# Sitecore .ashx, /document/<id>). The URL pass above leaves those 'unresolved' and without a year,
# so they show as "Company | Document" and company coverage does not count them (AmRest, Oct 2026:
# ~80 reports downloaded, 2025 annual report still "missing"). Once the file is attached and its
# cover text is read, the file name and the title page usually say exactly what the document is:
# "AmRest Group_Condensed consolidated interim report H1 2026.pdf". --from-file classifies those
# rows again from that text. Runs nightly after Build cover text (dedupe workflow).
F_FILE = "file"; F_COVER = "cover_text"; F_YEAR = "doc_year"
YEAR = re.compile(r"(?<!\d)(20[0-3]\d)(?!\d)")
YEAR_PHRASE = re.compile(r"(?:year|period|months|financial year|fiscal year|fy)\s+(?:ended|ending)?\s*"
                         r"(?:on\s+)?(?:\d{1,2}\s+)?(?:[a-z]+\s+)?(?:\d{1,2},?\s+)?(20[0-3]\d)", re.I)


def _name(f):
    from urllib.parse import unquote
    a = (f.get(F_FILE) or [{}])[0]
    return unquote(a.get("filename") or "")


def year_from(filename, cover, this_year=None):
    """Reporting year: from the file name first, else a 'year ended ... 2025' phrase in the cover,
    else the latest plausible year on the title page. '' when nothing credible is found."""
    import datetime as _dt
    this_year = this_year or _dt.date.today().year
    ok = lambda y: 2000 <= y <= this_year + 1
    ys = [int(y) for y in YEAR.findall(filename) if ok(int(y))]
    if ys:
        return str(max(ys))
    m = YEAR_PHRASE.search(cover[:3000])
    if m and ok(int(m.group(1))):
        return m.group(1)
    ys = [int(y) for y in YEAR.findall(cover[:400]) if ok(int(y))]
    return str(max(ys)) if ys else ""


def classify_from_file(filename, cover):
    """(source_type, confidence) from the file name, else from the title page (first 400 chars)."""
    t, c = classify("/" + filename.replace(" ", "-")) if filename else ("", "unresolved")
    if t:
        return t, "file name"
    head = re.sub(r"[^\w]+", "-", cover[:400].lower())
    t, c = classify("/" + head)
    return (t, "cover") if t else ("", "unresolved")


def from_file_rows(base, token, table):
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}"}
    formula = (f"AND({{{F_CONF}}}='unresolved', {{{F_COVER}}}!='', NOT({{discard}}))")
    params = [("pageSize", "100"), ("filterByFormula", formula)] + \
             [("fields[]", x) for x in (F_ID, F_URL, F_FILE, F_COVER, F_YEAR)]
    out = []; offset = None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        r = airtable_request("GET", url, headers, params=p); r.raise_for_status()
        j = r.json()
        out += j.get("records", [])
        offset = j.get("offset"); time.sleep(0.22)
        if not offset: break
    return out


def label_from_file(records):
    """updates for the rows the file name or title page can label; counters for the log."""
    updates, by = [], collections.Counter()
    for rec in records:
        f = rec.get("fields", {})
        name, cover = _name(f), f.get(F_COVER) or ""
        t, c = classify_from_file(name, cover)
        upd = {}
        if t:
            upd[F_TYPE] = t; upd[F_CONF] = c
        if not (f.get(F_YEAR) or "").strip():
            y = year_from(name, cover)
            if y:
                upd[F_YEAR] = y
        by[c if t else ("year only" if upd else "still unresolved")] += 1
        if upd:
            updates.append({"id": rec["id"], "fields": upd})
    return updates, by


def commit(base, token, table, updates):
    import requests
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    print(f"writing source_type/match_confidence on {len(updates)} rows ...")
    for i in range(0, len(updates), 10):
        r = airtable_request("PATCH", url, headers, {"records": updates[i:i + 10], "typecast": True})
        r.raise_for_status(); time.sleep(0.22)
        if i and i % 1000 == 0:
            print(f"  {i}/{len(updates)}")
    print("done.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["airtable", "csv"], default="airtable")
    ap.add_argument("--csv")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-classify rows that already have a type")
    ap.add_argument("--from-file", action="store_true",
                    help="label 'unresolved' rows from their attached file name and cover text")
    args = ap.parse_args()
    if args.from_file:
        token = os.environ.get("AIRTABLE_TOKEN"); base = os.environ.get("AIRTABLE_BASE")
        table = os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library")
        if not (token and base):
            sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
        recs = from_file_rows(base, token, table)
        updates, by = label_from_file(recs)
        print(f"label from file: {len(recs)} unresolved rows with cover text; {dict(by)}; "
              f"{len(updates)} rows to update.")
        if args.commit and updates:
            commit(base, token, table, updates)
        elif not args.commit:
            print("DRY-RUN. Re-run with --commit to write the labels.")
        return
    force = args.force or os.environ.get("CLASSIFY_FORCE", "").lower() in ("true", "1", "yes")

    by_type = collections.Counter(); by_conf = collections.Counter(); updates = []
    if args.source == "csv":
        if not args.csv:
            sys.exit("--csv PATH required with --source csv")
        for r in from_csv(args.csv):
            t, c = classify(r.get(F_URL, "") or r.get("url", ""))
            by_type[t or "(unresolved)"] += 1; by_conf[c] += 1
    else:
        token = os.environ.get("AIRTABLE_TOKEN"); base = os.environ.get("AIRTABLE_BASE")
        table = os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library")
        if not (token and base):
            sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
        rows = from_airtable(base, token, table, force)
        print(f"{len(rows)} rows to classify{' [FORCE]' if force else ' (missing source_type)'}.")
        for rid, doc in rows:
            t, c = classify(doc)
            by_type[t or "(unresolved)"] += 1; by_conf[c] += 1
            updates.append({"id": rid, "fields": {F_TYPE: t, F_CONF: c}})

    total = sum(by_type.values()) or 1
    print("=" * 56)
    print(f"classified: {total}")
    print(f"  confidence: {dict(by_conf)}")
    resolved = total - by_type.get('(unresolved)', 0)
    print(f"  got a source type: {resolved} ({resolved / total * 100:.1f}%)")
    print("  by source type:")
    for t, n in by_type.most_common():
        print(f"    {t}: {n}")
    print("=" * 56)

    if args.source == "airtable" and args.commit:
        commit(os.environ["AIRTABLE_BASE"], os.environ["AIRTABLE_TOKEN"],
               os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library"), updates)
    elif args.source == "airtable":
        print("\nDRY-RUN. Re-run with --commit to write the labels.")


if __name__ == "__main__":
    main()
