#!/usr/bin/env python3
"""Nightly analyst reconciliation: companies.analyst is the source of truth for
assigned_analyst on monitored_links and report_library.

Why this exists: assignment used to ride on three Airtable automations (one per-change
propagation, two per-record-created stamps). The July harvest influx created tens of
thousands of rows in days; per-row automation runs burned the workspace quota and all
automations went silent, leaving the same company with stamped and unstamped rows.
Per-event triggers do not survive bulk pipelines; an idempotent sweep does.

What it does, per table:
  - reads companies -> {wba_id: collaborator or None}
  - sweeps rows (wba_id, assigned_analyst), compares against the expected analyst
  - patches ONLY diffs: blank -> set, wrong -> corrected, and cleared when the
    company has no analyst. Rows already correct are never written.

Same contract as the sibling jobs: ids resolved first, orphan wba_ids logged and
skipped, dry-run by default, --commit writes.

Env: AIRTABLE_TOKEN, AIRTABLE_BASE, ANALYST_FIELD (default assigned_analyst).
"""
import argparse, os, time
from urllib.parse import quote
from monitor_core import airtable_request

API = "https://api.airtable.com/v0"
COMPANIES = "companies"
TABLES = ["report_library", "monitored_links"]
F_WBA = "wba_id"
F_ROW_ANALYST = os.environ.get("ANALYST_FIELD", "assigned_analyst")
F_CO_ANALYST = "analyst"


def sweep(base, token, table, fields):
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}"}
    params = [("pageSize", "100")] + [("fields[]", f) for f in fields]
    offset = None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", url, headers, params=p).json()
        yield from j.get("records", [])
        offset = j.get("offset")
        if not offset:
            return
        time.sleep(0.18)


def plan_updates(rows, analyst_map):
    """Pure: rows + {wba: usr_id|None} -> (batches, orphans, stats).
    A row is touched only when its analyst id differs from the company's."""
    updates, orphans = [], set()
    stats = {"set": 0, "fixed": 0, "cleared": 0, "ok": 0}
    for r in rows:
        f = r.get("fields", {})
        wba = (f.get(F_WBA) or "").strip()
        if not wba:
            continue
        if wba not in analyst_map:
            orphans.add(wba)
            continue
        want = analyst_map[wba]                       # usr id or None
        have = (f.get(F_ROW_ANALYST) or {}).get("id")  # usr id or None
        if have == want:
            stats["ok"] += 1
            continue
        if want is None:
            stats["cleared"] += 1
            updates.append({"id": r["id"], "fields": {F_ROW_ANALYST: None}})
        else:
            stats["fixed" if have else "set"] += 1
            updates.append({"id": r["id"], "fields": {F_ROW_ANALYST: {"id": want}}})
    return [updates[i:i + 10] for i in range(0, len(updates), 10)], orphans, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    base = os.environ["AIRTABLE_BASE"]
    token = os.environ["AIRTABLE_TOKEN"]

    analyst_map = {}
    for r in sweep(base, token, COMPANIES, [F_WBA, F_CO_ANALYST]):
        f = r.get("fields", {})
        w = (f.get(F_WBA) or "").strip()
        if w:
            analyst_map[w] = (f.get(F_CO_ANALYST) or {}).get("id")
    with_analyst = sum(1 for v in analyst_map.values() if v)
    print(f"companies: {len(analyst_map)} mapped, {with_analyst} with an analyst")

    for table in TABLES:
        rows = list(sweep(base, token, table, [F_WBA, F_ROW_ANALYST]))
        batches, orphans, st = plan_updates(rows, analyst_map)
        total = sum(len(b) for b in batches)
        print(f"{table}: {len(rows)} rows -> ok {st['ok']}, set {st['set']}, "
              f"fixed {st['fixed']}, cleared {st['cleared']}; orphans {len(orphans)}")
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
                print(f"  {table}: {done}/{total}", flush=True)
            time.sleep(0.21)
        print(f"{table}: DONE, {done} rows reconciled.")


if __name__ == "__main__":
    main()
