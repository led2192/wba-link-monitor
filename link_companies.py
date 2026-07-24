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

Env: AIRTABLE_TOKEN, AIRTABLE_BASE, COMPANY_LINK_FIELD (default "company").
"""
import argparse, os, time
from urllib.parse import quote
from monitor_core import airtable_request   # retrying Airtable helper

API = "https://api.airtable.com/v0"
COMPANIES = "companies"
LIBRARY = "report_library"
F_WBA = "wba_id"
F_LINK = os.environ.get("COMPANY_LINK_FIELD", "company")


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

    # Only rows with the link still empty: idempotent by construction.
    rows = list(sweep(base, token, LIBRARY, [F_WBA], formula=f"{{{F_LINK}}} = BLANK()"))
    print(f"report_library rows missing the link: {len(rows)}")

    batches, orphans = build_batches(rows, id_map, F_LINK)
    total = sum(len(b) for b in batches)
    print(f"to write: {total} rows in {len(batches)} batches; orphan wba_ids: {len(orphans)}")
    for w in sorted(orphans)[:10]:
        print("  orphan:", w)

    if not args.commit:
        print("DRY-RUN: nothing written. Re-run with --commit.")
        return

    url = f"{API}/{base}/{quote(LIBRARY)}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    done = 0
    for b in batches:
        airtable_request("PATCH", url, headers, {"records": b})
        done += len(b)
        if done % 1000 < 10:
            print(f"  linked {done}/{total}", flush=True)
        time.sleep(0.21)
    print(f"DONE: linked {done} documents to their company records.")


if __name__ == "__main__":
    main()
