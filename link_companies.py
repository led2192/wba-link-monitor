#!/usr/bin/env python3
"""One-time backfill + daily top-up: populate the report_library -> companies record link.

Why: the interfaces need the company card to embed its documents as a linked list,
and that requires a real record link, not a text wba_id. This script resolves each
document's wba_id to the companies record id and writes the link field.

Safety contract (same as link_documents.py, whose call pattern this mirrors verbatim):
  - record ids are resolved from companies first; nothing is ever written with
    typecast, so a wba_id that does not exist in companies is LOGGED and skipped,
    never fabricated (the phantom-rows lesson).
  - idempotent: only rows whose link field is empty are touched; re-runs converge.
  - dry-run by default; --commit writes.

Covers BOTH record links out of companies:
  report_library.company     (documents  -> companies), field env LIBRARY_LINK_FIELD
  monitored_links.companies  (pages      -> companies), field env PAGES_LINK_FIELD
The June build created the pages link FIELD but never populated it (verified empty on
2026-07-22), so both sides go through the same backfill + daily top-up.

Env: AIRTABLE_TOKEN, AIRTABLE_BASE, LIBRARY_LINK_FIELD, PAGES_LINK_FIELD.
"""
import argparse, os, time
from urllib.parse import quote
from monitor_core import airtable_request   # retrying Airtable helper

API = "https://api.airtable.com/v0"
COMPANIES = "companies"
F_WBA = "wba_id"
# (table, link field on that table pointing at companies)
TARGETS = [
    ("report_library", os.environ.get("LIBRARY_LINK_FIELD", "company")),
    ("monitored_links", os.environ.get("PAGES_LINK_FIELD", "companies")),
]


def sweep(base, token, table, fields, formula=None):
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}"}
    params = [("pageSize", "100")] + [("fields[]", f) for f in fields]
    if formula:
        params.append(("filterByFormula", formula))
    offset = None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", url, headers, params=p).json()
        yield from j.get("records", [])
        offset = j.get("offset")
        if not offset:
            return
        time.sleep(0.18)


def build_batches(rows, id_map, link_field):
    """Pure: (library rows, wba->rec map) -> (batches of PATCH records, orphan wba set)."""
    updates, orphans = [], set()
    for r in rows:
        wba = (r.get("fields", {}).get(F_WBA) or "").strip()
        rec = id_map.get(wba)
        if not rec:
            if wba:
                orphans.add(wba)
            continue
        updates.append({"id": r["id"], "fields": {link_field: [rec]}})
    return [updates[i:i + 10] for i in range(0, len(updates), 10)], orphans


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    base = os.environ["AIRTABLE_BASE"]
    token = os.environ["AIRTABLE_TOKEN"]

    id_map = {}
    for r in sweep(base, token, COMPANIES, [F_WBA]):
        w = (r.get("fields", {}).get(F_WBA) or "").strip()
        if w:
            id_map[w] = r["id"]
    print(f"companies: {len(id_map)} wba_ids mapped")

    for table, link_field in TARGETS:
        # Only rows with the link still empty: idempotent by construction.
        rows = list(sweep(base, token, table, [F_WBA], formula=f"{{{link_field}}} = BLANK()"))
        print(f"{table}: rows missing the link: {len(rows)}")

        batches, orphans = build_batches(rows, id_map, link_field)
        total = sum(len(b) for b in batches)
        print(f"{table}: to write {total} rows in {len(batches)} batches; orphan wba_ids: {len(orphans)}")
        for w in sorted(orphans)[:10]:
            print("  orphan:", w)

        if not args.commit:
            print(f"{table}: DRY-RUN, nothing written.")
            continue

        url = f"{API}/{base}/{quote(table)}"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        done = 0
        for b in batches:
            airtable_request("PATCH", url, headers, {"records": b})
            done += len(b)
            if done % 1000 < 10:
                print(f"  {table}: linked {done}/{total}", flush=True)
            time.sleep(0.21)
        print(f"{table}: DONE, linked {done} rows to their company records.")


if __name__ == "__main__":
    main()
