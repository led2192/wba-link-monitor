#!/usr/bin/env python3
"""
harvest_reports.py — build a library of EVERY report PDF, not just the newly-detected ones.

For the monitored_links rows whose `type` is in the chosen set (default: reports_hub,
sustainability_page, sustainability_report, investor_relations, policies), it takes every .pdf
URL sitting in seen_links (the full set of report/PDF links the monitor saw on that page, current
and historical) and writes one row per PDF per company into a separate table (default
report_library), upserting on a stable library_id so re-runs don't duplicate.

Separate from detections on purpose: detections is the stream of NEW events; this is the standing
corpus of "all the report PDFs on these pages". Keeping it in its own table is what keeps the
detections review queue small.

URL correctness: seen_links was historically stored as the normalize() key (lowercased, no
scheme, no www), which is why some of those URLs 404. The fixed monitor (monitor_core) now stores
the fetchable URL. So run the fixed monitor once over these pages first (a MONITOR_FORCE_ALL pass
refreshes seen_links to the fetchable form) and THEN harvest, to get download-ready URLs. The
report below tells you how many entries are still in the old key form. library_id is computed
from the normalized URL, so it is identical in both forms: re-harvesting after the refresh updates
the same rows in place.

Levers:
  --types reports_hub,investor_relations   restrict to a subset (default: all five)
  --reports-only                           keep only PDFs whose file name looks like a report
                                           (a year, or annual/sustainab/integrated/esg/report/...)

Modes:
  --source csv --csv monitored_links.csv   count what would be harvested (no writes)
  --source airtable                        read the live table (default); dry-run unless --commit
  --commit                                 actually upsert into the library table

Env (airtable source): AIRTABLE_TOKEN, AIRTABLE_BASE,
  AIRTABLE_TABLE (default monitored_links), AIRTABLE_LIBRARY_TABLE (default report_library)
"""
import argparse, os, re, sys, time, collections, hashlib
from urllib.parse import quote, urlsplit

API = "https://api.airtable.com/v0"
ALL_TYPES = ["reports_hub", "sustainability_page", "sustainability_report",
             "investor_relations", "policies"]

F_WBA = "wba_id"; F_NAME = "company_name"; F_URL = "url"; F_TYPE = "type"
F_SEEN = "seen_links"; F_IS_PDF = "is_pdf"
YEAR = re.compile(r"(20[12]\d)")
REPORT_NAME = re.compile(r"annual|sustainab|integrated|\besg\b|\bcsr\b|report|memoria|"
                         r"rapport|informe|bericht|disclosure|statement|20[12]\d", re.I)


def is_pdf_url(u):
    return urlsplit(u.split("?", 1)[0]).path.lower().endswith(".pdf")


def fetchable(u):
    """A seen_links entry -> a URL. Fetchable form (has scheme) kept as-is; an old normalize()
    key (no scheme) gets https:// prepended."""
    u = u.strip()
    if not u:
        return ""
    return u if u.startswith(("http://", "https://")) else "https://" + u


def year_of(u):
    ys = [int(y) for y in YEAR.findall(u or "") if 2010 <= int(y) <= 2027]
    return str(max(ys)) if ys else ""


def looks_like_report(u):
    name = urlsplit(u.split("?", 1)[0]).path.rsplit("/", 1)[-1]
    return bool(REPORT_NAME.search(name))


def pdfs_from_row(fields, types):
    """Yield (document_url, found_on) for every PDF this row contributes."""
    if str(fields.get(F_TYPE, "")).strip().lower() not in types:
        return
    page = fields.get(F_URL, "")
    for line in (fields.get(F_SEEN) or "").split("\n"):
        u = fetchable(line)
        if u and is_pdf_url(u):
            yield u, page
    if str(fields.get(F_IS_PDF, "")).strip().lower() in ("checked", "true", "1", "yes") or is_pdf_url(page):
        yield fetchable(page), page


def build(records, types, reports_only):
    """Returns deduped library rows + per-company / per-type counts. Dedup is by
    company+document, so a PDF linked from several pages is ONE library row."""
    from ids import normalize, link_id
    rows = {}
    per_company = collections.Counter()
    per_type = collections.Counter()
    for fields in records:
        wba = fields.get(F_WBA, ""); name = fields.get(F_NAME, ""); typ = fields.get(F_TYPE, "")
        for doc, page in pdfs_from_row(fields, types):
            if reports_only and not looks_like_report(doc):
                continue
            key = normalize(doc) or doc.strip().lower()
            lib_id = f"{(str(wba).strip() or 'NA')}-{hashlib.sha1(key.encode()).hexdigest()[:10]}"
            if lib_id not in rows:
                per_company[wba] += 1
                per_type[str(typ).strip().lower()] += 1
                rows[lib_id] = {"library_id": lib_id, "wba_id": wba, "company_name": name,
                                "document_url": doc, "found_on": page, "page_type": typ,
                                "doc_year": year_of(doc), "source_link_id": link_id(wba, page)}
    return list(rows.values()), per_company, per_type


def scheme_stats(records, types, reports_only):
    ok = bare = 0
    for fields in records:
        if str(fields.get(F_TYPE, "")).strip().lower() not in types:
            continue
        for line in (fields.get(F_SEEN) or "").split("\n"):
            u = line.strip()
            if u and is_pdf_url(fetchable(u)) and (not reports_only or looks_like_report(u)):
                ok += 1 if u.startswith(("http://", "https://")) else 0
                bare += 0 if u.startswith(("http://", "https://")) else 1
    return ok, bare


def report(rows, per_company, per_type, ok, bare):
    print("=" * 56)
    print(f"report PDFs harvested (unique per company): {len(rows)}")
    print(f"  across companies: {len(per_company)}")
    print(f"  seen_links entries already fetchable (correct URL): {ok}")
    print(f"  still in old key form (need a monitor refresh)     : {bare}")
    print("  by page type:")
    for t, n in per_type.most_common():
        print(f"    {t}: {n}")
    print("=" * 56)
    print("  top companies:")
    for wid, n in per_company.most_common(10):
        print(f"    {wid}: {n}")


def from_csv(path):
    import csv
    csv.field_size_limit(10_000_000)
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return [r for r in csv.DictReader(fh)]


def from_airtable(base, token, table, types):
    import requests
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}"}
    type_or = ", ".join(f"{{type}}='{t}'" for t in sorted(types))
    params = {"pageSize": 100, "filterByFormula": f"OR({type_or})"}
    out = []; offset = None
    while True:
        if offset: params["offset"] = offset
        r = requests.get(url, headers=headers, params=params, timeout=30); r.raise_for_status()
        j = r.json(); out.extend(rec.get("fields", {}) for rec in j.get("records", []))
        offset = j.get("offset"); time.sleep(0.22)
        if not offset: break
    return out


def commit(base, token, table, rows):
    import requests
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    print(f"upserting {len(rows)} library rows into '{table}' ...")
    for i in range(0, len(rows), 10):
        payload = {"performUpsert": {"fieldsToMergeOn": ["library_id"]},
                   "records": [{"fields": x} for x in rows[i:i + 10]], "typecast": True}
        r = requests.patch(url, headers=headers, json=payload, timeout=30)
        r.raise_for_status(); time.sleep(0.22)
        if i and i % 1000 == 0:
            print(f"  {i}/{len(rows)}")
    print("done.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["airtable", "csv"], default="airtable")
    ap.add_argument("--csv")
    ap.add_argument("--types", default=",".join(ALL_TYPES),
                    help="comma-separated page types to include")
    ap.add_argument("--reports-only", action="store_true")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    types = {t.strip().lower() for t in args.types.split(",") if t.strip()}

    if args.source == "csv":
        if not args.csv:
            sys.exit("--csv PATH required with --source csv")
        records = from_csv(args.csv)
    else:
        token = os.environ.get("AIRTABLE_TOKEN"); base = os.environ.get("AIRTABLE_BASE")
        table = os.environ.get("AIRTABLE_TABLE", "monitored_links")
        if not (token and base):
            sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
        records = from_airtable(base, token, table, types)

    rows, per_company, per_type = build(records, types, args.reports_only)
    ok, bare = scheme_stats(records, types, args.reports_only)
    report(rows, per_company, per_type, ok, bare)

    if args.source == "airtable" and args.commit:
        commit(os.environ["AIRTABLE_BASE"], os.environ["AIRTABLE_TOKEN"],
               os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library"), rows)
    elif args.source == "airtable":
        print("\nDRY-RUN. Re-run with --commit to write into the library table.")


if __name__ == "__main__":
    main()
