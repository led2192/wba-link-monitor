#!/usr/bin/env python3
"""
short_reports.py: a document of one or two pages is not a periodic report.

The AI field that types documents from their first pages called 7,443 documents of one or two pages an annual,
integrated or sustainability report, a 10-K or a registration document (15 Nov 2026; executive biographies of one
page came up as "Annual Report 2026"). This script sets `source_type_check` to UNKNOWN on those rows of
report_library, so that the effective type, the coverage numbers and every reader stop counting them as reports.
It sets robot_type (the pipeline's type snapshot) to UNKNOWN too, so that Pipeline QA does not take the change for a
person's correction.

Which rows: live (not discarded), not verified by a person, page_count 1 or 2, effective type one of REPORT_TYPES.
A row a person has verified is never touched. What the type was is kept in lanterN's database
(public.type_corrections: library_id, the type it had, where that type came from, when), so the change can be
read back and undone.

Dry run by default: prints the count by type and writes nothing. --commit writes, ten rows a call.
Running it again changes nothing more: a corrected row is UNKNOWN and no longer in the selection.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE; SUPABASE_DB_URL (--commit, for the record of what the type was).
"""
import argparse, collections, os, sys, time
from urllib.parse import quote

from load_supabase import REPORT_TYPES, SHORT_PAGES

UNKNOWN = "UNKNOWN"
# robot_type moves with the type: the change is the pipeline's own, not a person's correction. Writing only
# source_type_check made qa_sync record 5,043 corrections by hand on 16 Nov 2026 (Pipeline QA, LABEL_CORRECTION).
WRITE = {"source_type_check": UNKNOWN, "robot_type": UNKNOWN}
FIELDS = ["library_id", "source_type_check", "source_type", "page_count", "verified", "discard"]


def formula():
    types = ",".join("{effective_type}='%s'" % t for t in REPORT_TYPES)
    return f"AND(NOT({{discard}}), NOT({{verified}}), {{page_count}}>=1, {{page_count}}<={SHORT_PAGES}, OR({types}))"


def effective(f):
    """(type, where it comes from): the AI's or a person's source_type_check when set, else the address's."""
    check = (f.get("source_type_check") or "").strip()
    return (check, "source_type_check") if check else ((f.get("source_type") or "").strip(), "source_type")


def select(records):
    """The rows to correct, checked again here whatever the formula returned: [(record id, library_id, type, from)]."""
    out = []
    for r in records:
        f = r.get("fields", {})
        t, src = effective(f)
        pages = f.get("page_count") or 0
        if f.get("verified") or f.get("discard") or t not in REPORT_TYPES or not (1 <= pages <= SHORT_PAGES):
            continue
        out.append((r["id"], f.get("library_id") or "", t, src))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args(argv)
    for v in ("AIRTABLE_TOKEN", "AIRTABLE_BASE") + (("SUPABASE_DB_URL",) if a.commit else ()):
        if not os.environ.get(v):
            sys.exit(f"Set {v}.")
    import qa
    from monitor_core import airtable_request
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("short_reports.py")
    base, token = os.environ["AIRTABLE_BASE"], os.environ["AIRTABLE_TOKEN"]
    url = f"https://api.airtable.com/v0/{base}/{quote('report_library')}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    records, offset = [], None
    params = {"pageSize": 100, "filterByFormula": formula(), "fields[]": FIELDS}
    while True:
        if offset:
            params["offset"] = offset
        j = airtable_request("GET", url, headers, params=params).json()
        records += j.get("records", [])
        offset = j.get("offset")
        if not offset:
            break
    todo = select(records)
    if a.limit:
        todo = todo[:a.limit]
    by = collections.Counter(t for _, _, t, _ in todo)
    src = collections.Counter(s for _, _, _, s in todo)
    print(f"{len(todo):,} documents of 1 to {SHORT_PAGES} pages typed as a periodic report (live, not verified): "
          + ", ".join(f"{k} {v:,}" for k, v in by.most_common()))
    print(f"the type came from: the AI or a person's field {src['source_type_check']:,}, the address {src['source_type']:,}")
    written = 0
    if a.commit and todo:
        import evidence_db
        with evidence_db.connect() as conn:
            cur = conn.cursor()
            cur.execute("create table if not exists public.type_corrections (library_id text not null, record_id text not null, was text not null, "
                        "was_from text not null, became text not null, why text not null, at timestamptz not null default now(), primary key (record_id, at))")
            cur.execute("alter table public.type_corrections enable row level security")
            conn.commit()
            for i in range(0, len(todo), 10):
                part = todo[i:i + 10]
                # the record first: if the write to Airtable then fails, the worst case is a record of a change not made
                cur.executemany("insert into public.type_corrections (library_id, record_id, was, was_from, became, why) values (%s, %s, %s, %s, %s, %s)",
                                [(lid, rid, t, s, UNKNOWN, f"{SHORT_PAGES} pages or fewer") for rid, lid, t, s in part])
                conn.commit()
                airtable_request("PATCH", url, headers, payload={"records": [{"id": rid, "fields": WRITE} for rid, _, _, _ in part]})
                written += len(part)
                time.sleep(0.22)
                if (i // 10) % 100 == 99:
                    print(f"  {written:,} written", flush=True)
        print(f">>> {written:,} rows set to {UNKNOWN}; what each was is in public.type_corrections")
    else:
        print(">>> dry run: nothing written" if not a.commit else ">>> nothing to write")
    run.finish("ok", rows_in=len(records), rows_out=written, counters={"selected": len(todo), "written": written, **{k: v for k, v in by.items()}},
               notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
