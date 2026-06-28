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
    args = ap.parse_args()
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
