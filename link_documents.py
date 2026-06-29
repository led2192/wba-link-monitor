#!/usr/bin/env python3
"""
link_documents.py — ONE-TIME backfill: turn report_library.source_link_id (text) into a real
Airtable link to the monitored_links source page.

harvest already writes source_link_id on every document = the link_id of the monitored_links row
the document was found on. This walks report_library, looks each source_link_id up against
monitored_links' link_id, and sets the link field to that record. Matching is done with real
record ids (a {link_id: record_id} map), so a source_link_id that matches nothing is skipped,
never turned into a stray monitored_links row.

Idempotent: only fills rows whose link field is still empty, so it is safe to re-run and to wave
with --limit. Run it once after creating the link field, then you can delete this script.

Create first: a "Link to another record" field in report_library pointing to monitored_links
(default name 'source_page'; override with AIRTABLE_LINK_FIELD).

Env: AIRTABLE_TOKEN, AIRTABLE_BASE,
     AIRTABLE_LIBRARY_TABLE (default report_library), AIRTABLE_LINKS_TABLE (default monitored_links),
     AIRTABLE_LINK_FIELD (default source_page)
"""
import argparse, os, sys, time
from urllib.parse import quote
from monitor_core import airtable_request   # retrying Airtable helper

API = "https://api.airtable.com/v0"
F_SRCID = "source_link_id"      # report_library: the source page's link_id (text)
F_LINKID = "link_id"            # monitored_links: primary key


def link_id_to_record(base, token, links_table):
    """Map every monitored_links link_id -> its record id."""
    url = f"{API}/{base}/{quote(links_table)}"
    headers = {"Authorization": f"Bearer {token}"}
    params = [("pageSize", "100"), ("fields[]", F_LINKID)]
    out = {}; offset = None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", url, headers, params=p).json()
        for rec in j.get("records", []):
            lid = rec.get("fields", {}).get(F_LINKID)
            if lid:
                out[lid] = rec["id"]
        offset = j.get("offset"); time.sleep(0.15)
        if not offset:
            break
    return out


def rows_to_link(base, token, library_table, link_field, limit):
    """report_library rows with a source_link_id and an empty link field: (record_id, source_link_id)."""
    url = f"{API}/{base}/{quote(library_table)}"
    headers = {"Authorization": f"Bearer {token}"}
    params = [("pageSize", "100"), ("fields[]", F_SRCID), ("fields[]", link_field)]
    out = []; offset = None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", url, headers, params=p).json()
        for rec in j.get("records", []):
            f = rec.get("fields", {})
            if f.get(F_SRCID) and not f.get(link_field):
                out.append((rec["id"], f[F_SRCID]))
                if limit and len(out) >= limit:
                    return out
        offset = j.get("offset"); time.sleep(0.15)
        if not offset:
            break
    return out


def write_links(base, token, library_table, link_field, pairs):
    """pairs = [(record_id, source_record_id), ...]."""
    url = f"{API}/{base}/{quote(library_table)}"
    headers = {"Authorization": f"Bearer {token}"}
    recs = [{"id": rid, "fields": {link_field: [src_rid]}} for rid, src_rid in pairs]
    for i in range(0, len(recs), 10):
        airtable_request("PATCH", url, headers, {"records": recs[i:i + 10]})
        time.sleep(0.25)
        if i and i % 2000 == 0:
            print(f"  linked {i}/{len(recs)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("AIRTABLE_TOKEN"); base = os.environ.get("AIRTABLE_BASE")
    library_table = os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library")
    links_table = os.environ.get("AIRTABLE_LINKS_TABLE", "monitored_links")
    link_field = os.environ.get("AIRTABLE_LINK_FIELD", "source_page")
    if not (token and base):
        sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")

    id_map = link_id_to_record(base, token, links_table)
    print(f"{len(id_map)} link_id -> record mappings from '{links_table}'.")

    rows = rows_to_link(base, token, library_table, link_field, args.limit)
    print(f"{len(rows)} '{library_table}' rows with source_link_id and an empty '{link_field}'.")

    pairs, missing = [], 0
    for rid, src in rows:
        src_rid = id_map.get(src)
        if src_rid:
            pairs.append((rid, src_rid))
        else:
            missing += 1
    print(f"{len(pairs)} will be linked; {missing} have a source_link_id with no matching "
          f"monitored_links row (skipped).")

    if not args.commit:
        print("dry run: pass --commit to write the links.")
        return
    write_links(base, token, library_table, link_field, pairs)
    print(f"done. linked {len(pairs)} documents to their source page.")


if __name__ == "__main__":
    main()
