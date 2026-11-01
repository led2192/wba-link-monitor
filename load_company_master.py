#!/usr/bin/env python3
"""
load_company_master.py: WBA's SDG2000 company master list into the companies table.

The list (data/sdg2000_2025.csv, WBA's public SDG2000 list of 2025) gives each company's industry, ISIC
class, headquarters region and country, and ownership. The reader uses them to learn how companies of one
industry, region or country print their figures (1 Nov 2026: the model reader was right for 41% of India's
validated answers against 72% overall, and for 47% of the chemicals industry). WBA is redefining the list
with a new classification, so the load is re-runnable: run it again with the new file.

Rules: a row is matched by wba_id. wba_industry, isic_code and hq_region are written whenever the file
differs from the base (the file is the source). hq_country, ownership and isin are filled when blank and,
when they differ, reported and left alone (they may have been corrected by hand). Companies in the file but
not in the base, and in the base but not in the file, are reported, never created or deleted. Dry run by
default; --commit writes.

Env: AIRTABLE_TOKEN, AIRTABLE_BASE; AIRTABLE_QA_TOKEN, AIRTABLE_QA_BASE for the quality log.
"""
import argparse, collections, csv, os, sys, time

import qa

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CSV = os.path.join(HERE, "data", "sdg2000_2025.csv")
COLUMNS = {"WBA ID": "wba_id", "WBA Industry": "wba_industry", "ISIC Rev. 4": "isic_code", "Location HQ Region": "hq_region",
           "Location HQ ISO Code": "hq_country", "Ownership": "ownership", "ISIN": "isin", "WBA Company Name": "company_name"}
OVERWRITE = ("wba_industry", "isic_code", "hq_region")       # the file is the source
FILL = ("hq_country", "ownership", "isin")                   # filled when blank, reported when different
WRITE_PAUSE = 0.22


def read_master(path):
    """{wba_id: {field: value}} from the CSV, values stripped, blanks dropped."""
    out = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            f = {COLUMNS[k]: (v or "").strip() for k, v in row.items() if k in COLUMNS}
            if f.get("wba_id"):
                out[f["wba_id"]] = {k: v for k, v in f.items() if v}
    return out


def plan(master, rows):
    """(updates, report): updates = [{"id", "fields"}] for rows whose fields change; report = counters and
    the differences left alone. rows: [{"id", "fields": {...}}] of the companies table."""
    c = collections.Counter()
    updates, differs, missing_in_file = [], [], []
    seen = set()
    for r in rows:
        f = r["fields"]
        w = (f.get("wba_id") or "").strip()
        if not w:
            continue
        m = master.get(w)
        if not m:
            missing_in_file.append(w); continue
        seen.add(w)
        upd = {}
        for k in OVERWRITE:
            if m.get(k) and m[k] != (f.get(k) or "").strip():
                upd[k] = m[k]
        for k in FILL:
            have = (f.get(k) or "").strip()
            if m.get(k) and not have:
                upd[k] = m[k]
            elif m.get(k) and have and have != m[k]:
                differs.append((w, k, have, m[k])); c[f"{k} differs, left alone"] += 1
        if upd:
            updates.append({"id": r["id"], "fields": upd})
            for k in upd:
                c[f"{k} written"] += 1
    missing_in_base = sorted(set(master) - seen)
    c["companies in the file"] = len(master)
    c["companies matched"] = len(seen)
    c["in the base, not in the file"] = len(missing_in_file)
    c["in the file, not in the base"] = len(missing_in_base)
    c["rows to update"] = len(updates)
    return updates, {"counters": c, "differs": differs, "missing_in_file": missing_in_file, "missing_in_base": missing_in_base}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    from monitor_core import airtable_request
    from propose_values import fetch
    base, H = os.environ["AIRTABLE_BASE"], {"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}"}
    run = qa.Run("load_company_master.py")
    master = read_master(a.csv)
    rows = fetch(H, base, "companies", ["wba_id", "company_name"] + list(OVERWRITE) + list(FILL))
    updates, rep = plan(master, rows)
    c = rep["counters"]
    print(f">>> {len(master):,} companies in {os.path.basename(a.csv)}; {len(rows):,} rows in companies; {len(updates):,} to update"
          + ("" if a.commit else " (dry run: nothing written)"), flush=True)
    for k, v in sorted(c.items()):
        print(f"  {k}: {v:,}")
    for w, k, have, new in rep["differs"][:30]:
        print(f"  left alone: {w} {k}: base '{have}', file '{new}'")
    if rep["missing_in_file"]:
        print("  in the base, not in the file: " + ", ".join(rep["missing_in_file"][:40]))
    if rep["missing_in_base"]:
        print("  in the file, not in the base: " + ", ".join(rep["missing_in_base"][:40]))
    written = 0
    if a.commit:
        hdr = {**H, "Content-Type": "application/json"}
        for i in range(0, len(updates), 10):
            airtable_request("PATCH", f"https://api.airtable.com/v0/{base}/companies", hdr,
                             payload={"records": updates[i:i + 10], "typecast": False})
            written += len(updates[i:i + 10]); time.sleep(WRITE_PAUSE)
    c["rows written"] = written
    run.finish("ok", rows_in=len(rows), rows_out=written, counters=dict(c))
    return 0


if __name__ == "__main__":
    sys.exit(main())
