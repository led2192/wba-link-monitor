#!/usr/bin/env python3
"""
attach_files.py — pull the actual PDF into an Airtable attachment field, from document_url.

Airtable downloads a file when you write an attachment cell as [{"url": ...}]; this script does
that for report_library rows. It is deliberately SELECTIVE: attaching all ~167k is a very long,
rate-limited job and most historical PDFs 404, so by default it attaches only rows that already
have a source_type (i.e. classified) and have no attachment yet, capped by --limit. Widen with
--types / --year-min, or --all to attach every row missing a file.

Read this before a big run:
  - Attach AFTER the seen_links refresh. Old-form URLs (no scheme/www, the 3M case) won't fetch.
  - Dead links are skipped: Airtable just leaves that cell empty, the run continues.
  - Airtable downloads asynchronously and rate-limits attachment-by-URL, so this paces itself and
    retries. Use --limit to attach in waves rather than all at once.

Modes:
  --source airtable           default; dry-run unless --commit
  --commit                    actually write the attachments
  --types A,B                 only these source_types
  --year-min 2024             only doc_year >= this
  --limit N                   cap how many rows to attach this run (0 = no cap)
  --all                       ignore the source_type filter; attach any row missing a file

Env: AIRTABLE_TOKEN, AIRTABLE_BASE, AIRTABLE_LIBRARY_TABLE (default report_library),
     AIRTABLE_ATTACH_FIELD (default file)
"""
import argparse, os, sys, time
from urllib.parse import quote

API = "https://api.airtable.com/v0"
F_ID = "library_id"; F_URL = "document_url"; F_TYPE = "source_type"; F_YEAR = "doc_year"


def airtable_request(method, url, headers, payload=None, params=None, timeout=60, tries=5):
    import requests
    delay = 2
    for attempt in range(1, tries + 1):
        try:
            r = requests.request(method, url, headers=headers, json=payload, params=params, timeout=timeout)
        except requests.exceptions.RequestException:
            if attempt == tries:
                raise
            time.sleep(delay); delay = min(delay * 2, 30); continue
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == tries:
                r.raise_for_status()
            time.sleep(delay); delay = min(delay * 2, 30); continue
        r.raise_for_status()
        return r
    return r


def select_rows(base, token, table, attach_field, types, year_min, attach_all):
    """Rows missing the attachment that pass the filters: (record_id, document_url)."""
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}"}
    fields = [F_ID, F_URL, F_TYPE, F_YEAR, attach_field]
    params = [("pageSize", "100")] + [("fields[]", f) for f in fields]
    out = []; offset = None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        r = airtable_request("GET", url, headers, params=p)
        j = r.json()
        for rec in j.get("records", []):
            f = rec.get("fields", {})
            doc = f.get(F_URL, "")
            if not doc or f.get(attach_field):           # no url, or already attached
                continue
            if not attach_all and not f.get(F_TYPE):      # default: only classified rows
                continue
            if types and str(f.get(F_TYPE, "")) not in types:
                continue
            if year_min and (not str(f.get(F_YEAR, "")).isdigit() or int(f[F_YEAR]) < year_min):
                continue
            out.append((rec["id"], doc))
        offset = j.get("offset"); time.sleep(0.2)
        if not offset: break
    return out


def attach(base, token, table, attach_field, rows):
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    print(f"attaching {len(rows)} files into '{table}.{attach_field}' ...")
    done = 0
    for i in range(0, len(rows), 10):
        batch = rows[i:i + 10]
        payload = {"records": [{"id": rid, "fields": {attach_field: [{"url": doc}]}}
                               for rid, doc in batch], "typecast": True}
        airtable_request("PATCH", url, headers, payload)
        done += len(batch); time.sleep(0.5)            # pace attachment-by-URL
        if done % 500 == 0:
            print(f"  {done}/{len(rows)}")
    print("done. Airtable fetches the files in the background; some may take a few minutes.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["airtable"], default="airtable")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--types", default="")
    ap.add_argument("--year-min", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--all", action="store_true", help="attach any row missing a file, not just classified ones")
    args = ap.parse_args()

    token = os.environ.get("AIRTABLE_TOKEN"); base = os.environ.get("AIRTABLE_BASE")
    table = os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library")
    attach_field = os.environ.get("AIRTABLE_ATTACH_FIELD", "file")
    if not (token and base):
        sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
    types = {t.strip() for t in args.types.split(",") if t.strip()}

    rows = select_rows(base, token, table, attach_field, types, args.year_min, args.all)
    print(f"{len(rows)} rows selected (missing a file"
          f"{', any' if args.all else ', classified only'}"
          f"{', types=' + ','.join(sorted(types)) if types else ''}"
          f"{', year>=' + str(args.year_min) if args.year_min else ''}).")
    if args.limit and len(rows) > args.limit:
        rows = rows[:args.limit]
        print(f"capped to --limit {args.limit} this run.")

    if args.commit:
        attach(base, token, table, attach_field, rows)
    else:
        print("\nDRY-RUN. Re-run with --commit to attach. Start with a --limit to test a wave first.")


if __name__ == "__main__":
    main()
