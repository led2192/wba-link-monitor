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
import argparse, os, re, sys, time, collections, hashlib, datetime as dt
from urllib.parse import quote, urlsplit

API = "https://api.airtable.com/v0"
TODAY_ISO = dt.date.today().isoformat()
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
                slid = link_id(wba, page)
                # source_link_id stays as a text audit trail. source_page (the real link) is NOT set
                # here from a string: a string + typecast makes Airtable fabricate an empty
                # monitored_links row whenever slid does not match an existing page. It is resolved to
                # a real record id, by URL, at write time in main().
                rows[lib_id] = {"library_id": lib_id, "wba_id": wba, "company_name": name,
                                "document_url": doc, "found_on": page, "page_type": typ,
                                "doc_year": year_of(doc), "source_link_id": slid}
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
    """Returns (list_of_field_dicts, {normalized_url: record_id}) for rows whose type is in `types`.
    The url->record_id map lets harvest link each document to its source page by RECORD ID, matched
    on the page's normalized URL. That is drift-proof: it does NOT rely on the page's stored link_id
    equalling link_id(wba, url). Pages added by the sweep/discovered lanes can carry a link_id that
    no longer matches their url, and matching by URL sidesteps that entirely (and, crucially, stops
    Airtable's typecast from fabricating an empty monitored_links row for an unmatched id)."""
    from ids import normalize
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}"}
    type_or = ", ".join(f"{{type}}='{t}'" for t in sorted(types))
    params = {"pageSize": 100, "filterByFormula": f"OR({type_or})"}
    out = []; id_by_url = {}; offset = None
    while True:
        if offset: params["offset"] = offset
        r = _request("GET", url, headers, params=params)
        j = r.json()
        for rec in j.get("records", []):
            f = rec.get("fields", {})
            out.append(f)
            u = f.get(F_URL, "")
            if u:
                id_by_url[normalize(u)] = rec["id"]
        offset = j.get("offset"); time.sleep(0.22)
        if not offset: break
    return out, id_by_url


def existing_library(base, token, table):
    """{library_id: {"url": document_url, "first_seen": first_seen, "id": record_id}} already in
    the library, so a re-run writes only new/changed rows (by record id) and preserves first_seen."""
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}"}
    base_params = [("pageSize", "100"), ("fields[]", "library_id"),
                   ("fields[]", "document_url"), ("fields[]", "first_seen")]
    out = {}; offset = None
    while True:
        p = list(base_params) + ([("offset", offset)] if offset else [])
        r = _request("GET", url, headers, params=p)
        j = r.json()
        for rec in j.get("records", []):
            f = rec.get("fields", {})
            if f.get("library_id"):
                out[f["library_id"]] = {"url": f.get("document_url", ""),
                                        "first_seen": f.get("first_seen", ""),
                                        "id": rec["id"]}
        offset = j.get("offset"); time.sleep(0.22)
        if not offset: break
    return out


def _request(method, url, headers, payload=None, params=None, timeout=60, tries=5):
    """Airtable request (GET/POST/PATCH) with retry/backoff on timeouts, 429 and 5xx, so a single
    slow or rate-limited response during a long run does not crash it. Prints Airtable's error body
    on a real 4xx so the cause (bad field, bad value) is visible in the log."""
    import requests
    delay = 2
    for attempt in range(1, tries + 1):
        try:
            r = requests.request(method, url, headers=headers, json=payload, params=params, timeout=timeout)
        except requests.exceptions.RequestException:
            if attempt == tries:
                raise
            time.sleep(delay); delay = min(delay * 2, 30); continue
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == tries:
                print(f"Airtable {method} {r.status_code}: {r.text[:400]}")
                r.raise_for_status()
            time.sleep(delay); delay = min(delay * 2, 30); continue
        if r.status_code >= 400:
            print(f"Airtable {method} {r.status_code}: {r.text[:400]}")
        r.raise_for_status()
        return r
    return r


def write_records(base, token, table, records, method):
    """method='PATCH' updates existing rows (records carry 'id'); method='POST' creates new ones.
    These are DIRECT record-id writes, not performUpsert, so each call is fast regardless of how
    big the table is (no server-side match against the whole table)."""
    if not records:
        return
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    verb = "updating" if method == "PATCH" else "creating"
    print(f"{verb} {len(records)} rows in '{table}' ...")
    for i in range(0, len(records), 10):
        _request(method, url, headers, {"records": records[i:i + 10], "typecast": True})
        time.sleep(0.2)
        if i and i % 2000 == 0:
            print(f"  {i}/{len(records)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["airtable", "csv"], default="airtable")
    ap.add_argument("--csv")
    ap.add_argument("--types", default=",".join(ALL_TYPES),
                    help="comma-separated page types to include")
    ap.add_argument("--reports-only", action="store_true")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--full", action="store_true",
                    help="re-upsert every row; default only writes new/changed rows")
    args = ap.parse_args()
    types = {t.strip().lower() for t in args.types.split(",") if t.strip()}

    id_by_url = {}                       # normalized page url -> monitored_links record id
    if args.source == "csv":
        if not args.csv:
            sys.exit("--csv PATH required with --source csv")
        records = from_csv(args.csv)
    else:
        token = os.environ.get("AIRTABLE_TOKEN"); base = os.environ.get("AIRTABLE_BASE")
        table = os.environ.get("AIRTABLE_TABLE", "monitored_links")
        if not (token and base):
            sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
        records, id_by_url = from_airtable(base, token, table, types)

    rows, per_company, per_type = build(records, types, args.reports_only)
    ok, bare = scheme_stats(records, types, args.reports_only)
    report(rows, per_company, per_type, ok, bare)

    if args.source == "airtable" and args.commit:
        from ids import normalize
        base = os.environ["AIRTABLE_BASE"]; token = os.environ["AIRTABLE_TOKEN"]
        lib_table = os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library")

        # Resolve each document's source page to a REAL monitored_links record id, matched on the
        # page's normalized URL. No string ever enters the source_page link field, so typecast can
        # never fabricate a page. A document whose page is not in the table is left unlinked
        # (source_page absent) rather than pointed at a fabricated row.
        linked = unlinked = 0
        for r in rows:
            rid = id_by_url.get(normalize(r.get("found_on", "")))
            if rid:
                r["source_page"] = [rid]; linked += 1
            else:
                unlinked += 1
        print(f"source_page resolved by URL: {linked} linked, {unlinked} with no matching page "
              f"(left unlinked).")

        existing = existing_library(base, token, lib_table)
        to_update = []; to_create = []
        for r in rows:
            prev = existing.get(r["library_id"])
            if prev:
                r["first_seen"] = prev.get("first_seen") or TODAY_ISO   # keep original date
                if args.full or prev.get("url") != r["document_url"]:
                    to_update.append({"id": prev["id"], "fields": r})
            else:
                r["first_seen"] = TODAY_ISO
                to_create.append({"fields": r})
        print(f"{len(rows)} harvested; {len(existing)} already in '{lib_table}'. "
              f"writing {len(to_create)} new + {len(to_update)} changed (direct record writes).")
        write_records(base, token, lib_table, to_create, "POST")
        write_records(base, token, lib_table, to_update, "PATCH")
    elif args.source == "airtable":
        print("\nDRY-RUN. Re-run with --commit to write into the library table.")


if __name__ == "__main__":
    main()
