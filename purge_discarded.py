#!/usr/bin/env python3
"""
purge_discarded.py — permanently delete report_library rows that were discarded long ago.

Discarding keeps a row in the table, and every row counts toward the Airtable record limit. This
job turns old discards into real deletions, safely:

  1. selects rows with discard ticked, discarded_on older than --days (default 30), never verified
  2. writes an ARCHIVE of those rows (every field, one JSON object per line, gzipped). The workflow
     attaches it to a GitHub Release, so a deleted row can always be looked up again
  3. adds each row to the TOMBSTONE list (state/tombstones_report_library.tsv.gz): company + document
     key. harvest_reports.py reads this list and never re-creates a tombstoned document. Without it,
     a deleted duplicate would come back the next night because its URL is still on the company page
  4. only then deletes, 10 rows per request

Phases (the workflow runs them in this order and stops if any step fails):
  --prepare   steps 1-3, writes purge_ids.txt; nothing is deleted
  --delete    step 4, for the ids in purge_ids.txt

The 30-day wait is the undo window: unticking discard resets discarded_on and saves the row.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE, AIRTABLE_LIBRARY_TABLE (default report_library)
"""
import argparse, collections, gzip, json, os, sys, time
from datetime import date
from urllib.parse import quote

import requests

from ids import doc_id

API = "https://api.airtable.com/v0"
TOKEN = os.environ.get("AIRTABLE_TOKEN"); BASE = os.environ.get("AIRTABLE_BASE")
TABLE = os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library")
H = {"Authorization": f"Bearer {TOKEN}"}
TOMBSTONES = "state/tombstones_report_library.tsv.gz"
IDS_FILE = "purge_ids.txt"


def req(method, url, **kw):
    delay = 2
    for attempt in range(6):
        try:
            r = requests.request(method, url, headers=H, timeout=60, **kw)
        except requests.exceptions.RequestException:
            if attempt == 5: raise
            time.sleep(delay); delay = min(delay * 2, 30); continue
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == 5: r.raise_for_status()
            time.sleep(delay); delay = min(delay * 2, 30); continue
        r.raise_for_status()
        return r


# Reasons written by the robots (dedupe_library.py). Rows discarded for these reasons may be purged
# without the 7-day wait (--machine-only): the duplicate keeps its twin in the library, the archive
# keeps every field, and a row can be restored from the archive if a rule was wrong.
MACHINE_REASONS = ("Duplicate of another document", "Not a disclosure")


def candidates(days, machine_only=False):
    formula = (f"AND({{discard}}, NOT({{verified}}), "
               f"IS_BEFORE({{discarded_on}}, DATEADD(NOW(), -{int(days)}, 'days')))")
    if machine_only:
        reasons = ", ".join(f"{{discard_reason}}='{r}'" for r in MACHINE_REASONS)
        formula = (f"AND({{discard}}, NOT({{verified}}), OR({reasons}), "
                   f"IS_BEFORE({{discarded_on}}, DATEADD(NOW(), -{int(days)}, 'days')))")
    url = f"{API}/{BASE}/{quote(TABLE)}"
    offset = None
    while True:
        p = [("pageSize", "100"), ("filterByFormula", formula)] + ([("offset", offset)] if offset else [])
        j = req("GET", url, params=p).json()
        yield from j.get("records", [])
        offset = j.get("offset"); time.sleep(0.12)
        if not offset:
            break


def load_tombstones(path=TOMBSTONES):
    """{doc_id} of every document ever purged. Used by harvest_reports.py."""
    if not os.path.exists(path):
        return set()
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return {line.split("\t", 1)[0] for line in fh if line.strip()}


def prepare(days, limit, machine_only=False):
    today = date.today().isoformat()
    rows = []
    for rec in candidates(days, machine_only):
        rows.append(rec)
        if limit and len(rows) >= limit:
            break
    by_reason = collections.Counter(r["fields"].get("discard_reason") or "(no reason)" for r in rows)
    print(f">>> {len(rows):,} rows discarded more than {days} days ago (cap {limit or 'none'}). "
          f"By reason: {dict(by_reason)}", flush=True)

    archive = f"report_library_purge_{today}.jsonl.gz"
    with gzip.open(archive, "wt", encoding="utf-8") as fh:
        for r in rows:
            f = dict(r["fields"])
            if f.get("file"):   # attachment URLs expire; keep what identifies the file
                f["file"] = [{"filename": a.get("filename"), "size": a.get("size")} for a in f["file"]]
            fh.write(json.dumps({"record_id": r["id"], "createdTime": r.get("createdTime"),
                                 "fields": f}, ensure_ascii=False) + "\n")

    os.makedirs(os.path.dirname(TOMBSTONES), exist_ok=True)
    known = load_tombstones()
    new = 0
    lines = []
    if os.path.exists(TOMBSTONES):
        with gzip.open(TOMBSTONES, "rt", encoding="utf-8") as fh:
            lines = [l.rstrip("\n") for l in fh if l.strip()]
    for r in rows:
        f = r["fields"]
        wba = f.get("wba_id") or str(f.get("library_id", "")).rsplit("-", 1)[0]
        d = doc_id(wba, f.get("document_url", ""))
        if d not in known:
            known.add(d); new += 1
            lines.append(f"{d}\t{f.get('library_id', '')}\t{today}\t{f.get('discard_reason') or ''}")
    with gzip.open(TOMBSTONES, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + ("\n" if lines else ""))

    with open(IDS_FILE, "w") as fh:
        fh.write("\n".join(r["id"] for r in rows) + ("\n" if rows else ""))
    print(f">>> archive: {archive} | tombstones: +{new:,} (total {len(lines):,}) | ids: {IDS_FILE}",
          flush=True)


def delete():
    ids = [l.strip() for l in open(IDS_FILE) if l.strip()]
    url = f"{API}/{BASE}/{quote(TABLE)}"
    done = 0
    for i in range(0, len(ids), 10):
        chunk = ids[i:i + 10]
        req("DELETE", url, params=[("records[]", x) for x in chunk])
        done += len(chunk); time.sleep(0.22)
        if (i // 10) % 500 == 0:
            print(f"  ... deleted {done:,}/{len(ids):,}", flush=True)
    print(f">>> deleted {done:,} rows.", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prepare", action="store_true")
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=0, help="cap rows per run (0 = no cap)")
    ap.add_argument("--machine-only", action="store_true",
                    help="only rows the robots discarded as duplicates / non-disclosures; no minimum wait")
    a = ap.parse_args()
    if not (TOKEN and BASE):
        sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
    if a.days < 7 and not a.machine_only:
        sys.exit("--days below 7 is refused: the wait is the undo window "
                 "(except --machine-only, robot duplicates and non-disclosures).")
    if a.prepare:
        prepare(a.days, a.limit, a.machine_only)
    if a.delete:
        delete()
    if not (a.prepare or a.delete):
        sys.exit("Pass --prepare and/or --delete.")


if __name__ == "__main__":
    main()
