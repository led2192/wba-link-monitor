#!/usr/bin/env python3
"""
doc_fixes.py: what the owner corrects in the admin list "Documents to fetch" is applied to the library.

The list shows documents whose address is held and whose file could not be read. Opening one, the owner sometimes
finds it is not what its row says (an "Integrated Report 2024" that is a sustainability report of 2016), or that it
should not be in the library at all (a dead address, a page that is not a report, the same document twice). Until
19 Nov 2026 the only way to say so was to open Airtable. Now each row has two actions; each writes one line to
public.doc_fixes in the evidence database, and this script applies the lines to report_library:

    discard   discard = true and discard_reason = "admin: <reason>" (never deleted; undone by unticking in Airtable)
    relabel   source_type_check = the type given (one of source_type_terms.json), doc_year = the year given

A line is applied once: applied_at and result are written back, also when the row could not be found (the document
is recognised by its record id as the evidence database holds it, else by its library_id).

    python doc_fixes.py [--commit] [--limit 200]

Dry run prints what it would do: counts by action, never a title or an address. Env: SUPABASE_DB_URL,
AIRTABLE_TOKEN, AIRTABLE_BASE; AIRTABLE_QA_TOKEN for the run record. Run by the Scan now watch (side_jobs) and by
hand (workflow Document fixes).
"""
import argparse, collections, json, os, sys, time
from urllib.parse import quote

REASONS = ("dead link", "not a report", "duplicate", "wrong company", "other")
TYPES = set(json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "source_type_terms.json"), encoding="utf-8")))

WAITING_SQL = """
select f.id, f.library_id, f.action, f.doc_type, f.year, f.reason, d.record_id
from public.doc_fixes f left join public.documents d on d.library_id = f.library_id
where f.applied_at is null order by f.id limit %s"""


def fields_for(action, doc_type=None, year=None, reason=None):
    """The fields of report_library a fix writes, or None with why not. No link field among them."""
    if action == "discard":
        r = (reason or "other").strip().lower()
        return ({"discard": True, "discard_reason": "admin: " + (r if r in REASONS else "other")}, None)
    if action == "relabel":
        f = {}
        if doc_type:
            if doc_type not in TYPES:
                return None, "not one of the library's types"
            f["source_type_check"] = doc_type
        if year is not None:
            if not 1990 <= int(year) <= 2100:
                return None, "not a year"
            f["doc_year"] = str(int(year))
        return (f, None) if f else (None, "nothing to change")
    return None, "not an action"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=200)
    a = ap.parse_args()
    for v in ("SUPABASE_DB_URL", "AIRTABLE_TOKEN", "AIRTABLE_BASE"):
        if not os.environ.get(v):
            sys.exit(f"Set {v}.")
    import qa, evidence_db
    from base_health import sweep, API
    from monitor_core import airtable_request
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("doc_fixes.py")
    c = collections.Counter()
    url = f"{API}/{os.environ['AIRTABLE_BASE']}/{quote('report_library')}"
    headers = {"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}", "Content-Type": "application/json"}
    with evidence_db.connect() as conn:
        cur = conn.cursor()
        cur.execute("select to_regclass('public.doc_fixes') is not null")
        if not cur.fetchone()[0]:
            print("public.doc_fixes is not there yet: nothing to do.")
            run.finish("ok", rows_in=0, rows_out=0, notes="no table")
            return 0
        cur.execute(WAITING_SQL, (a.limit,))
        waiting = cur.fetchall()

        def done(fix_id, result):
            cur.execute("update public.doc_fixes set applied_at = now(), result = %s where id = %s and applied_at is null", (result[:300], fix_id))
            conn.commit()

        for fix_id, lid, action, doc_type, year, reason, rec in waiting:
            fields, why = fields_for(action, doc_type, year, reason)
            c[action if fields else "refused"] += 1
            if not a.commit:
                continue
            if not fields:
                done(fix_id, "not applied: " + why); continue
            if not rec:                                          # the evidence database does not know its record: look the row up
                rows = list(sweep("report_library", ["library_id"], "{library_id}='%s'" % (lid or "").replace("'", "")))
                rec = rows[0]["id"] if rows else None
            if not rec:
                c["no row"] += 1
                run.failure("DATA_CHECK", "a fix from the admin list names a document the library does not hold", key=f"doc_fix|{fix_id}")
                done(fix_id, "not applied: the library holds no row for this document"); continue
            try:
                airtable_request("PATCH", f"{url}/{rec}", headers, payload={"fields": fields, "typecast": True})   # no link field among them
                done(fix_id, "applied: " + ", ".join(sorted(fields)))
                c["applied"] += 1
            except Exception as e:                               # left waiting: tried again on the next run
                c["failed"] += 1
                run.failure("DATA_CHECK", f"a fix from the admin list was not written ({type(e).__name__})", key=f"doc_fix|{fix_id}")
            time.sleep(0.25)
    print(f"{len(waiting)} fixes waiting: {c['discard']} discard, {c['relabel']} relabel, {c['refused']} refused; "
          f"{c['applied']} applied, {c['no row']} with no row, {c['failed']} failed" + ("" if a.commit else " (dry run: nothing written)"), flush=True)
    run.finish("ok", rows_in=len(waiting), rows_out=c["applied"], counters=dict(c), notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
