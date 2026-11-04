#!/usr/bin/env python3
"""
load_guidance.py: load attribute guidance (how to read each attribute) into the Disclosure Library table
attribute_guidance.

The guidance comes from WBA's methodology documents (ITA Social and Nature indicator and attribute
development drafts, Nov 2026; Climate to come), parsed to one JSON record per attribute: dimension, area,
indicator, indicator statement, attribute, type, options, unit, entity type, null codes, description,
collection guidance, evaluation guidance. The documents are internal drafts, so the JSON lives in the private
repository wba-text-store, whose workflow runs this script; this public repository holds no methodology text.

Idempotent: rows are matched on guidance_id (dimension | indicator | attribute); a reload updates them and
adds new ones. A row a person corrected (edited ticked) is left alone. Nothing is deleted: an attribute that
disappears from the documents is reported, not removed. Dry run by default; --commit writes.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE. Usage: load_guidance.py FILE.json [--source NAME] [--status draft|final] [--commit]
"""
import argparse, json, os, sys, time
from urllib.parse import quote

import qa
from monitor_core import airtable_request

API = "https://api.airtable.com/v0"
TABLE = "attribute_guidance"
TYPES = {"boolean", "multiple select", "single select", "number"}
AREAS = {"Planning", "Implementation", "Performance"}


def guidance_id(r):
    return " | ".join((r.get(k) or "").strip() for k in ("dimension", "indicator", "attribute"))


def fields_of(r, source, status):
    """The Airtable fields of one parsed record (long texts cut at Airtable's 100,000 characters)."""
    cut = lambda s: (s or "").strip()[:99000]
    t = (r.get("type_normalised") or "").strip()
    f = {"guidance_id": guidance_id(r), "dimension": r.get("dimension") or None,
         "area": r.get("area") if r.get("area") in AREAS else None,
         "indicator": cut(r.get("indicator")), "indicator_statement": cut(r.get("indicator_statement")),
         "attribute": cut(r.get("attribute")), "type": t if t in TYPES else "?",
         "type_as_written": cut(r.get("type")), "options": cut(r.get("options")), "unit": cut(r.get("unit")),
         "entity_type": cut(r.get("entity type")), "null_codes": cut(r.get("null codes")),
         "description": cut(r.get("description")), "collection_guidance": cut(r.get("collection guidance")),
         "evaluation_guidance": cut(r.get("evaluation guidance")), "source_document": source, "status": status}
    return {k: v for k, v in f.items() if v not in (None, "")}


def plan(records, existing, source, status):
    """(creates, updates, kept, gone): existing is {guidance_id: {"id", "edited"}}."""
    creates, updates, kept, seen = [], [], 0, set()
    for r in records:
        f = fields_of(r, source, status)
        gid = f["guidance_id"]
        if gid in seen:
            continue                                   # the same attribute twice in the documents: the first wins
        seen.add(gid)
        ex = existing.get(gid)
        if not ex:
            creates.append(f)
        elif ex.get("edited"):
            kept += 1
        else:
            updates.append((ex["id"], f))
    gone = sorted(set(existing) - seen)
    return creates, updates, kept, gone


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--source", default="")
    ap.add_argument("--status", default="draft", choices=["draft", "final"])
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("load_guidance.py")
    base, H = os.environ["AIRTABLE_BASE"], {"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}"}
    records = json.load(open(a.file, encoding="utf-8"))
    url = f"{API}/{base}/{quote(TABLE)}"
    existing, offset = {}, None
    while True:
        p = [("pageSize", "100"), ("fields[]", "guidance_id"), ("fields[]", "edited")] + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", url, H, params=p).json()
        for rec in j.get("records", []):
            existing[rec["fields"].get("guidance_id", "")] = {"id": rec["id"], "edited": rec["fields"].get("edited")}
        offset = j.get("offset")
        if not offset:
            break
        time.sleep(0.2)
    creates, updates, kept, gone = plan(records, existing, a.source or os.path.basename(a.file), a.status)
    print(f">>> {len(records):,} records in {a.file}: {len(creates):,} new, {len(updates):,} to update, "
          f"{kept:,} left alone (edited by hand), {len(gone):,} in the table but no longer in the file"
          + ("" if a.commit else " (dry run)"), flush=True)
    for g in gone[:20]:
        print(f"  no longer in the documents: {g}")
    if a.commit:
        hj = dict(H, **{"Content-Type": "application/json"})
        for i in range(0, len(creates), 10):
            airtable_request("POST", url, hj, {"records": [{"fields": f} for f in creates[i:i + 10]], "typecast": True})
            time.sleep(0.22)
        for i in range(0, len(updates), 10):
            airtable_request("PATCH", url, hj, {"records": [{"id": rid, "fields": f} for rid, f in updates[i:i + 10]], "typecast": True})
            time.sleep(0.22)
    run.finish("ok", rows_in=len(records), rows_out=len(creates) + len(updates),
               counters={"new": len(creates), "updated": len(updates), "kept": kept, "gone": len(gone)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
