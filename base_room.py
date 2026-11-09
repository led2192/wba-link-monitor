#!/usr/bin/env python3
"""
base_room.py: where the room in the Disclosure Library goes, and which documents could be archived.

The base holds at most 500,000 records (RECORD_LIMIT) and stood at 84% on 9 Nov 2026, flat since 6 Nov because
the harvest adds little while the reading jobs add attribute_values rows. Before anyone decides to archive
documents, this report says, from the base itself and never from memory:
  1. records per table, and the share of the limit each takes;
  2. report_library: live against discarded rows, and the purge backlog (discarded more than PURGE_DAYS ago);
  3. live documents by year and by kind (a key type as doc_map.KEY_TYPES lists them, another type, no type),
     with how many are cited by an attribute_values row, verified by a person, or imported from the Source
     Library (the analysts' own collection: never archive candidates);
  4. live documents without a usable file (blocked, gone, not_pdf, too_big) by age;
  5. archive candidates under four rules, each counted on its own and all together, with the room they would
     free and the days of intake that room buys at the last 30 days' rate.

The rules (a document is a candidate only when NO attribute_values row cites it, nobody verified it, it did
not come from the Source Library, and it is not a company's current key report):
  A  reporting year CUTOFF_YEAR (2021) or earlier, whatever the type;
  B  rule A and not a key type (the stricter reading of A);
  C  no year and no usable type (UNKNOWN or blank), first seen more than STALE_DAYS (180) ago;
  D  no usable file (file_status blocked, gone, not_pdf or too_big), first seen more than STALE_DAYS ago.
Archiving means copying the rows to another base and deleting them here with a job of its own, after the
owner's decision; this script writes NOTHING to Airtable. It writes base_room.md (the report) and
base_room.csv (library_id, rules, year, type) so the decision is made on the list, not on a number.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE, RECORD_LIMIT (default 500,000), PURGE_DAYS (default 30).
"""
import argparse, collections, csv, os, sys, time
from datetime import date, datetime, timedelta, timezone

RECORD_LIMIT = int(os.environ.get("RECORD_LIMIT") or 500_000)
PURGE_DAYS = int(os.environ.get("PURGE_DAYS") or 30)
CUTOFF_YEAR = 2021
STALE_DAYS = 180
NO_FILE = {"blocked", "gone", "not_pdf", "too_big"}
TABLES = ["report_library", "attribute_values", "webpage_library", "monitored_links", "companies", "mention_searches",
          "mention_hits", "company_contacts", "attribute_guidance", "base_health"]
DOC_FIELDS = ["library_id", "wba_id", "year", "source_type_check", "file_status", "discard", "discarded_on", "verified",
              "first_seen", "origin", "key_role", "page_count", "file_size_mb"]
YEAR_GROUPS = ["no year", "2019 or earlier", "2020", "2021", "2022", "2023", "2024", "2025", "2026"]


def key_types():
    from doc_map import KEY_TYPES
    return set(KEY_TYPES)


def year_of(f):
    y = f.get("year")
    if isinstance(y, (int, float)) and 1990 <= int(y) <= 2100:
        return int(y)
    if isinstance(y, str) and y.strip()[:4].isdigit():
        return int(y.strip()[:4])
    return None


def year_group(y):
    if y is None:
        return "no year"
    if y <= 2019:
        return "2019 or earlier"
    return str(min(y, 2026))


def kind_of(f, keys):
    t = (f.get("source_type_check") or "").strip()
    if not t or t == "UNKNOWN":
        return "no type"
    return "key type" if t in keys else "other type"


def days_since(s, today):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except ValueError:
        return None
    return (today - d).days


def rules_for(f, cited, keys, today):
    """The archive rules this live document meets, as a string of letters ('' when none)."""
    if f.get("discard") or f.get("verified") or f.get("key_role") or f.get("library_id") in cited:
        return ""
    if (f.get("origin") or "").startswith("Source Library"):
        return ""
    y, kind = year_of(f), kind_of(f, keys)
    age = days_since(f.get("first_seen") or f.get("created_time"), today)
    stale = age is not None and age > STALE_DAYS
    out = ""
    if y is not None and y <= CUTOFF_YEAR:
        out += "A"
        if kind != "key type":
            out += "B"
    if y is None and kind == "no type" and stale:
        out += "C"
    if (f.get("file_status") or "") in NO_FILE and stale:
        out += "D"
    return out


def analyse(docs, cited, keys, today):
    """Counters for the report and the candidate rows. docs: [{fields}] of report_library."""
    c = collections.Counter()
    by = collections.defaultdict(collections.Counter)     # year group -> kind / cited / verified / imported counts
    nofile = collections.Counter()
    cands = []
    for f in docs:
        c["rows"] += 1
        if f.get("discard"):
            c["discarded"] += 1
            age = days_since(f.get("discarded_on"), today)
            if age is not None and age > PURGE_DAYS:
                c["purge_backlog"] += 1
            continue
        c["live"] += 1
        g, kind = year_group(year_of(f)), kind_of(f, keys)
        by[g]["total"] += 1
        by[g][kind] += 1
        if f.get("library_id") in cited:
            by[g]["cited"] += 1; c["cited"] += 1
        if f.get("verified"):
            by[g]["verified"] += 1
        if (f.get("origin") or "").startswith("Source Library"):
            by[g]["imported"] += 1
        age = days_since(f.get("first_seen") or f.get("created_time"), today)
        if age is not None and age <= 30:
            c["new_30d"] += 1
        if (f.get("file_status") or "") in NO_FILE:
            nofile["all"] += 1
            if age is not None and age > STALE_DAYS:
                nofile["stale"] += 1
        r = rules_for(f, cited, keys, today)
        if r:
            for letter in r:
                c["rule_" + letter] += 1
            c["candidates"] += 1
            cands.append((f.get("library_id") or "", r, year_of(f) or "", (f.get("source_type_check") or ""), f.get("wba_id") or ""))
    return c, by, nofile, cands


def pct(n, d):
    return f"{n / d:.1%}" if d else "0%"


def report_md(tables, c, by, nofile, cands, today):
    total = sum(tables.values())
    md = [f"# Room in the Disclosure Library ({today.isoformat()})", "",
          f"{total:,} records of {RECORD_LIMIT:,} ({pct(total, RECORD_LIMIT)}). Nothing here was written to Airtable; the "
          f"candidate list is base_room.csv.", "", "## Records per table", "", "| Table | Records | Share of the limit |", "| --- | --: | --: |"]
    md += [f"| {t} | {n:,} | {pct(n, RECORD_LIMIT)} |" for t, n in sorted(tables.items(), key=lambda x: -x[1])]
    md += ["", "## report_library", "",
           f"{c['live']:,} live rows and {c['discarded']:,} discarded ({c['purge_backlog']:,} discarded more than {PURGE_DAYS} days ago, "
           f"which the purge job can delete). {c['cited']:,} live documents are cited by an attribute_values row. "
           f"{c['new_30d']:,} live documents arrived in the last 30 days ({c['new_30d'] / 30:,.0f} a day).", "",
           "## Live documents by year and kind", "",
           "| Year | Documents | Key type | Other type | No type | Cited | Verified | Source Library |",
           "| --- | --: | --: | --: | --: | --: | --: | --: |"]
    for g in YEAR_GROUPS:
        b = by.get(g, collections.Counter())
        md.append(f"| {g} | {b['total']:,} | {b['key type']:,} | {b['other type']:,} | {b['no type']:,} | {b['cited']:,} | {b['verified']:,} | {b['imported']:,} |")
    md += ["", "## Live documents without a usable file", "",
           f"{nofile['all']:,} live documents have file_status blocked, gone, not_pdf or too_big; {nofile['stale']:,} of them were first seen "
           f"more than {STALE_DAYS} days ago and are still without a file.", "",
           "## Archive candidates", "",
           "A candidate is a live document that no attribute_values row cites, nobody verified, that did not come from the Source Library "
           "and is not a company's current key report. The rules overlap; the last line is their union.", "",
           "| Rule | Documents | Share of the limit | Days of intake freed |", "| --- | --: | --: | --: |"]
    rate = c["new_30d"] / 30
    rows = [("A: reporting year %d or earlier" % CUTOFF_YEAR, c["rule_A"]), ("B: rule A and not a key type", c["rule_B"]),
            ("C: no year and no type, first seen over %d days ago" % STALE_DAYS, c["rule_C"]),
            ("D: no usable file, first seen over %d days ago" % STALE_DAYS, c["rule_D"]), ("All rules together", c["candidates"])]
    md += [f"| {name} | {n:,} | {pct(n, RECORD_LIMIT)} | {(f'{n / rate:,.0f}' if rate else 'no intake in 30 days')} |" for name, n in rows]
    after = total - c["candidates"]
    md += ["", f"After archiving every candidate the base would hold {after:,} records ({pct(after, RECORD_LIMIT)}). Rule B is the "
           f"cautious choice (old and not a key report); rule A also archives old annual and sustainability reports, which the readers "
           f"use as other-year columns and which the anchor (9 Nov 2026) reads for the previous year's figure, so A should wait for the "
           f"anchor's adoption decision.", ""]
    return "\n".join(md)


def write_csv(cands, path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["library_id", "rules", "year", "source_type_check", "wba_id"])
        w.writerows(sorted(cands))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="base_room")
    a = ap.parse_args()
    if not (os.environ.get("AIRTABLE_TOKEN") and os.environ.get("AIRTABLE_BASE")):
        sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
    import qa
    from base_health import count_rows, sweep
    run = qa.Run("base_room.py")
    today = datetime.now(timezone.utc).date()
    t0 = time.time()
    keys = key_types()
    tables = {}
    for t in TABLES:
        if t in ("report_library", "attribute_values"):
            continue
        tables[t] = count_rows(t)
        print(f"  {t}: {tables[t]:,}", flush=True)
    cited = set()
    tables["attribute_values"] = 0
    for rec in sweep("attribute_values", ["library_id"]):
        tables["attribute_values"] += 1
        lid = rec.get("fields", {}).get("library_id")
        if lid:
            cited.add(lid)
    print(f"  attribute_values: {tables['attribute_values']:,} rows citing {len(cited):,} documents", flush=True)
    docs = [rec.get("fields", {}) | {"created_time": rec.get("createdTime")} for rec in sweep("report_library", DOC_FIELDS)]
    tables["report_library"] = len(docs)
    print(f"  report_library: {len(docs):,} rows in {time.time() - t0:.0f}s", flush=True)
    c, by, nofile, cands = analyse(docs, cited, keys, today)
    md = report_md(tables, c, by, nofile, cands, today)
    open(a.out + ".md", "w", encoding="utf-8").write(md)
    write_csv(cands, a.out + ".csv")
    print(md)
    run.finish("ok", rows_in=len(docs), rows_out=0, counters=dict(c),
               notes=f"{sum(tables.values()):,} records; {c['candidates']:,} archive candidates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
