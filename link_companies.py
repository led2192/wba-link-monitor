#!/usr/bin/env python3
"""One-time backfill: populate the report_library -> companies record link.

Why: the interfaces need the company card to embed its documents as a linked list,
and that requires a real record link, not a text wba_id. This script resolves each
document's wba_id to the companies record id and writes the link field.

Safety contract (same as link_documents.py):
  - record ids are resolved from companies first; nothing is ever written with
    typecast, so a wba_id that does not exist in companies is LOGGED and skipped,
    never fabricated (the phantom-rows lesson).
  - idempotent: only rows whose link field is empty are touched; re-runs converge.
  - dry-run by default; --commit writes.

Usage:
  python link_companies.py                # dry-run: counts and samples only
  python link_companies.py --commit
  COMPANY_LINK_FIELD=company python link_companies.py --commit
"""
import os, sys, time, argparse
from monitor_core import API, airtable_request

BASE = os.environ["AIRTABLE_BASE"]
COMPANIES = "companies"
LIBRARY = "report_library"
F_WBA = "wba_id"
F_LINK = os.environ.get("COMPANY_LINK_FIELD", "company")


def sweep(table, fields, formula=None):
    params = {"pageSize": 100, "fields[]": fields}
    if formula:
        params["filterByFormula"] = formula
    offset = None
    while True:
        p = dict(params)
        if offset:
            p["offset"] = offset
        data = airtable_request("GET", f"{API}/{BASE}/{table}", params=p)
        yield from data.get("records", [])
        offset = data.get("offset")
        if not offset:
            return
        time.sleep(0.22)


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

    id_map = {}
    for r in sweep(COMPANIES, [F_WBA]):
        w = (r.get("fields", {}).get(F_WBA) or "").strip()
        if w:
            id_map[w] = r["id"]
    print(f"companies: {len(id_map)} wba_ids mapped")

    # Only rows with the link still empty: idempotent by construction.
    formula = f"{{{F_LINK}}} = BLANK()"
    rows = list(sweep(LIBRARY, [F_WBA], formula))
    print(f"report_library rows missing the link: {len(rows)}")

    batches, orphans = build_batches(rows, id_map, F_LINK)
    total = sum(len(b) for b in batches)
    print(f"to write: {total} rows in {len(batches)} batches; orphan wba_ids: {len(orphans)}")
    for w in sorted(orphans)[:10]:
        print("  orphan:", w)

    if not args.commit:
        print("DRY-RUN: nothing written. Re-run with --commit.")
        return

    done = 0
    for b in batches:
        airtable_request("PATCH", f"{API}/{BASE}/{LIBRARY}", json={"records": b})
        done += len(b)
        if done % 1000 < 10:
            print(f"  linked {done}/{total}", flush=True)
        time.sleep(0.21)
    print(f"DONE: linked {done} documents to their company records.")


if __name__ == "__main__":
    main()
