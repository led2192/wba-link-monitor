#!/usr/bin/env python3
"""
company_coverage.py — daily per-company coverage summary written into `companies`.

Aggregates monitored_links and report_library by wba_id and writes the numbers onto the
matching `companies` row, so the companies table doubles as the coverage dashboard
("what might be missing"). This replaces the native link+rollup approach on purpose:
populating a link field across ~250k rows via typecast against a table whose primary
field is NOT wba_id is exactly the phantom-row recipe already hit once with SAIL.
This script resolves real record ids and never creates companies rows.

Fields written on `companies` (create them first; see company_coverage.yml header):
    urls_total          -> monitored_links rows for the company
    urls_monitored      -> of those, rows with monitor ticked
    docs_total          -> report_library rows (discarded rows excluded)
    docs_with_file      -> of those, rows with a file attachment
    docs_typed          -> of those, rows with an effective type (not blank / UNKNOWN)
    latest_report_year  -> max {year} over periodic report types
    coverage_breakdown  -> one line per effective type: "Sustainability Report: 14 (latest 2025)"
    coverage_flags      -> semicolon gap flags ("no_reports_hub; no_sust_report_2024+"); blank = healthy
    coverage_updated    -> date the stats last CHANGED (unchanged rows are not rewritten)

Effective type = source_type_check (AI) when set, else source_type (keyword classifier).
UNKNOWN and blank count as untyped, but UNKNOWN still shows in the breakdown.
Rows with discard ticked are excluded from every count.

Writes are diffed against current values: only rows whose stats changed are PATCHed,
so the daily chained run is quiet and last-modified on companies stays meaningful.
wba_ids present in the data but absent from companies are reported, never created.

Modes:
    (no flag)   dry run: aggregate everything, print totals + gap summary, write nothing
    --commit    write changed rows to companies

Env: AIRTABLE_TOKEN, AIRTABLE_BASE,
     AIRTABLE_LINKS_TABLE     (default monitored_links),
     AIRTABLE_LIBRARY_TABLE   (default report_library),
     AIRTABLE_COMPANIES_TABLE (default companies)
"""
import os, re, time, argparse
from datetime import date
from urllib.parse import quote

from monitor_core import airtable_request

TOKEN = os.environ["AIRTABLE_TOKEN"]
BASE  = os.environ["AIRTABLE_BASE"]
LINKS = os.environ.get("AIRTABLE_LINKS_TABLE", "monitored_links")
LIB   = os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library")
COMP  = os.environ.get("AIRTABLE_COMPANIES_TABLE", "companies")

API = "https://api.airtable.com/v0"
H   = {"Authorization": f"Bearer {TOKEN}"}
WRITE_BATCH = 10

# monitored_links fields
F_WBA = "wba_id"; F_MON = "monitor"; F_PTYPE = "type"
# report_library fields
F_TYPE_AI = "source_type_check"; F_TYPE_KW = "source_type"; F_YEAR = "year"
F_FILE = "file"; F_DISCARD = "discard"
# companies fields (the 9 stat fields this script owns)
C_WBA = "wba_id"
STAT_FIELDS = ["urls_total", "urls_monitored", "docs_total", "docs_with_file",
               "docs_typed", "latest_report_year", "coverage_breakdown", "coverage_flags"]
C_UPDATED = "coverage_updated"

# Effective-type families for recency flags. Names match source_type_check options verbatim.
SUST_FAMILY   = {"Sustainability Report", "ESG Report", "CSR Report",
                 "Integrated Report", "Impact Report"}
ANNUAL_FAMILY = {"Annual Report", "Integrated Report", "10K Form"}
REPORT_TYPES  = SUST_FAMILY | ANNUAL_FAMILY | {
    "Climate Report", "Environmental Reports", "Social Reports", "Interim Reports",
    "Financial Statement", "Registration Document", "CDP Report", "GRI",
    "SASB Index", "Engagement Report"}

YEAR_OK = re.compile(r"^20[0-3][0-9]$")   # sanity guard on the {year} formula output


def sweep(table, fields, label):
    """Yield the fields dict of every row in a table, requesting only what we aggregate."""
    url = f"{API}/{BASE}/{quote(table)}"
    params = [("pageSize", "100")] + [("fields[]", f) for f in fields]
    offset = None; n = 0
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", url, H, params=p).json()
        for rec in j.get("records", []):
            n += 1
            yield rec
        offset = j.get("offset")
        if n % 20_000 < 100:
            print(f"    {label}: {n:,} rows swept ...", flush=True)
        time.sleep(0.12)
        if not offset:
            break
    print(f"    {label}: {n:,} rows total.", flush=True)


def eff_type(f):
    """source_type_check first, keyword source_type as fallback. Returns '' when neither helps."""
    t = (f.get(F_TYPE_AI) or "").strip()
    if not t:
        t = (f.get(F_TYPE_KW) or "").strip()
    return t


def aggregate():
    """One pass over each big table -> {wba_id: stats dict}."""
    agg = {}

    def co(wba):
        return agg.setdefault(wba, {
            "urls_total": 0, "urls_monitored": 0, "page_types": set(),
            "docs_total": 0, "docs_with_file": 0, "docs_typed": 0,
            "type_counts": {}, "type_latest": {}, "latest_report_year": None})

    print(">>> sweeping monitored_links ...", flush=True)
    for rec in sweep(LINKS, [F_WBA, F_MON, F_PTYPE], "monitored_links"):
        f = rec.get("fields", {})
        wba = (f.get(F_WBA) or "").strip()
        if not wba:
            continue
        c = co(wba)
        c["urls_total"] += 1
        if f.get(F_MON):
            c["urls_monitored"] += 1
        pt = (f.get(F_PTYPE) or "").strip()
        if pt:
            c["page_types"].add(pt)

    print(">>> sweeping report_library ...", flush=True)
    for rec in sweep(LIB, [F_WBA, F_TYPE_AI, F_TYPE_KW, F_YEAR, F_FILE, F_DISCARD], "report_library"):
        f = rec.get("fields", {})
        wba = (f.get(F_WBA) or "").strip()
        if not wba or f.get(F_DISCARD):
            continue
        c = co(wba)
        c["docs_total"] += 1
        if f.get(F_FILE):
            c["docs_with_file"] += 1
        t = eff_type(f)
        if t:
            c["type_counts"][t] = c["type_counts"].get(t, 0) + 1
            if t != "UNKNOWN":
                c["docs_typed"] += 1
        y = f.get(F_YEAR)
        if isinstance(y, (int, float)) and YEAR_OK.match(str(int(y))):
            y = int(y)
            if t:
                if y > c["type_latest"].get(t, 0):
                    c["type_latest"][t] = y
                if t in REPORT_TYPES and y > (c["latest_report_year"] or 0):
                    c["latest_report_year"] = y
    return agg


def compose(c, year_min):
    """Stats dict -> the field values to sit on the companies row."""
    lines = []
    for t, n in sorted(c["type_counts"].items(), key=lambda kv: (-kv[1], kv[0])):
        y = c["type_latest"].get(t)
        lines.append(f"{t}: {n} (latest {y})" if y else f"{t}: {n}")
    breakdown = "\n".join(lines)

    def family_current(family):
        return any(c["type_latest"].get(t, 0) >= year_min for t in family)

    flags = []
    if c["urls_total"] == 0:
        flags.append("no_urls")
    elif c["urls_monitored"] == 0:
        flags.append("nothing_monitored")
    if "reports_hub" not in c["page_types"]:
        flags.append("no_reports_hub")
    if "sustainability_page" not in c["page_types"]:
        flags.append("no_sustainability_page")
    if c["docs_total"] == 0:
        flags.append("no_docs")
    else:
        if c["docs_with_file"] == 0:
            flags.append("no_files")
        if c["docs_typed"] == 0:
            flags.append("nothing_typed")
    if not family_current(SUST_FAMILY):
        flags.append(f"no_sust_report_{year_min}+")
    if not family_current(ANNUAL_FAMILY):
        flags.append(f"no_annual_report_{year_min}+")

    return {"urls_total": c["urls_total"], "urls_monitored": c["urls_monitored"],
            "docs_total": c["docs_total"], "docs_with_file": c["docs_with_file"],
            "docs_typed": c["docs_typed"], "latest_report_year": c["latest_report_year"],
            "coverage_breakdown": breakdown, "coverage_flags": "; ".join(flags)}


def empty_stats():
    return {"urls_total": 0, "urls_monitored": 0, "page_types": set(), "docs_total": 0,
            "docs_with_file": 0, "docs_typed": 0, "type_counts": {}, "type_latest": {},
            "latest_report_year": None}


def companies_index():
    """{wba_id: (record_id, current stat field values)} for the diff."""
    out = {}
    for rec in sweep(COMP, [C_WBA] + STAT_FIELDS, "companies"):
        f = rec.get("fields", {})
        wba = (f.get(C_WBA) or "").strip()
        if wba:
            out[wba] = (rec["id"], f)
    return out


def _norm(v):
    """Blank string, 0-as-float and None all compare stably. PATCHing None clears a field,
    so a target of None vs a stored value correctly counts as a change."""
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def changed(current, target):
    """True when any stat field differs from what is already on the row."""
    return any(_norm(current.get(k)) != _norm(target.get(k)) for k in STAT_FIELDS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write changed rows to companies")
    ap.add_argument("--year-min", type=int, default=2024,
                    help="a report family counts as current when its latest year >= this")
    args = ap.parse_args()

    # companies goes FIRST: it is the cheapest sweep (~2k rows) and the only one whose
    # fields might not exist yet. A missing stat field 422s here in seconds instead of
    # after the ~17-minute aggregation of the two big tables (learned the hard way on
    # the first live run, which died on UNKNOWN_FIELD_NAME at minute 17).
    print(">>> sweeping companies ...", flush=True)
    try:
        comp = companies_index()
    except Exception:
        print(">>> companies needs these fields (exact names): "
              + ", ".join([C_WBA] + STAT_FIELDS + [C_UPDATED]), flush=True)
        raise

    agg = aggregate()

    orphans = sorted((w for w in agg if w not in comp),
                     key=lambda w: -agg[w]["docs_total"])
    if orphans:
        print(f">>> WARN: {len(orphans)} wba_ids in the data have NO companies row "
              f"(never auto-created). Top by docs: "
              + ", ".join(f"{w}({agg[w]['docs_total']})" for w in orphans[:10]), flush=True)

    today = date.today().isoformat()
    to_write, unchanged, gap_count = [], 0, 0
    for wba, (rid, current) in comp.items():
        target = compose(agg.get(wba) or empty_stats(), args.year_min)
        if target["coverage_flags"]:
            gap_count += 1
        if changed(current, target):
            to_write.append({"id": rid, "fields": {**target, C_UPDATED: today}})
        else:
            unchanged += 1

    print(f">>> companies: {len(comp):,} | changed: {len(to_write):,} | unchanged: {unchanged:,} "
          f"| with gap flags: {gap_count:,}", flush=True)

    if not args.commit:
        for r in to_write[:5]:
            print(f"    sample -> {r['fields']}", flush=True)
        print(">>> dry run, nothing written. Re-run with --commit.", flush=True)
        return

    url = f"{API}/{BASE}/{quote(COMP)}"
    failed = 0
    for i in range(0, len(to_write), WRITE_BATCH):
        chunk = to_write[i:i + WRITE_BATCH]
        try:
            airtable_request("PATCH", url, H, {"records": chunk})
        except Exception as e:
            failed += len(chunk)
            print(f">>> WARN: batch write failed after retries ({e}); "
                  f"rows stay stale until the next run.", flush=True)
        time.sleep(0.12)
    print(f">>> done. written: {len(to_write) - failed:,} | failed: {failed:,}", flush=True)


if __name__ == "__main__":
    main()
