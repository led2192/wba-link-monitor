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

from monitor_core import is_doc_endpoint   # single source of truth for platform doc endpoints

API = "https://api.airtable.com/v0"
TODAY_ISO = dt.date.today().isoformat()
ALL_TYPES = ["reports_hub", "sustainability_page", "sustainability_report",
             "investor_relations", "policies"]

F_WBA = "wba_id"; F_NAME = "company_name"; F_URL = "url"; F_TYPE = "type"
F_SEEN = "seen_links"; F_IS_PDF = "is_pdf"
YEAR = re.compile(r"(20[12]\d)")
REPORT_NAME = re.compile(r"annual|sustainab|integrated|\besg\b|\bcsr\b|report|memoria|"
                         r"rapport|informe|bericht|disclosure|statement|20[12]\d", re.I)


# Document URLs confirmed by probing (doc_probe.py opens report-ish links that are not PDF-shaped):
# normalized key -> URL to harvest ('' = the link itself; a direct file URL when the link is a
# viewer page on an asset platform, e.g. Widen).
CONFIRMED_DOCS = {}


def _norm(u):
    from ids import normalize
    return normalize(u)


def is_pdf_url(u):
    path = urlsplit(u.split("?", 1)[0]).path.lower()
    if path.endswith(".pdf") or is_doc_endpoint(path):
        return True
    if CONFIRMED_DOCS:
        from ids import normalize
        return normalize(u) in CONFIRMED_DOCS
    return False


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
            target = CONFIRMED_DOCS.get(_norm(u)) if CONFIRMED_DOCS else None
            yield (target or u), page
    if str(fields.get(F_IS_PDF, "")).strip().lower() in ("checked", "true", "1", "yes") or is_pdf_url(page):
        yield fetchable(page), page


SHARED_DOC_MIN = int(os.environ.get("SHARED_DOC_MIN", "5") or 5)


def drop_shared_docs(rows):
    """Remove documents linked from >= SHARED_DOC_MIN distinct companies. With cross-domain
    PDFs admitted by default (monitor_core v3 rule), an unknown third-party PDF that many
    company pages link (an industry framework, a lobby report) would otherwise become one
    library row PER company. A document under that many companies is nobody's disclosure;
    same logic as unmonitor_shared, applied at document level. Returns (kept, skipped_summary)."""
    from ids import normalize
    per_doc = collections.defaultdict(set)
    for r in rows.values():
        per_doc[normalize(r["document_url"]) or r["document_url"]].add(r["wba_id"])
    shared = {k for k, wbas in per_doc.items() if len(wbas) >= SHARED_DOC_MIN}
    if not shared:
        return rows, []
    kept = {lid: r for lid, r in rows.items()
            if (normalize(r["document_url"]) or r["document_url"]) not in shared}
    summary = sorted(((len(per_doc[k]), k) for k in shared), reverse=True)
    return kept, summary


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
    rows, shared_summary = drop_shared_docs(rows)
    if shared_summary:
        print(f">>> shared-document guard: skipped {len(shared_summary)} documents linked by "
              f">={SHARED_DOC_MIN} companies (SHARED_DOC_MIN). Top offenders:")
        for n, k in shared_summary[:10]:
            print(f"      {n} companies -> {k}")
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
    # Harvest only pages that are still monitored. Unmonitoring a page (monitor=false) now also drops
    # it from harvest's input, so its seen_links are not read and its documents stop being (re-)ingested
    # into report_library. Previously harvest filtered purely by type and ignored the monitor flag, so
    # unmonitored pages kept feeding the library.
    params = {"pageSize": 100, "filterByFormula": f"AND({{monitor}}=TRUE(), OR({type_or}))"}
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


def existing_library(base, token, table, formula=None):
    """{library_id: {"url": document_url, "first_seen": first_seen, "id": record_id, "wba": wba_id}}
    already in the library, so a re-run writes only new/changed rows (by record id) and preserves
    first_seen. Discarded rows are included on purpose: their doc_id still blocks re-creation."""
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}"}
    base_params = [("pageSize", "100"), ("fields[]", "library_id"),
                   ("fields[]", "document_url"), ("fields[]", "first_seen"), ("fields[]", "wba_id")]
    if formula:
        base_params.append(("filterByFormula", formula))
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
                                        "id": rec["id"],
                                        "wba": f.get("wba_id") or f["library_id"].rsplit("-", 1)[0]}
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
    out = []
    if not records:
        return out
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    verb = "updating" if method == "PATCH" else "creating"
    print(f"{verb} {len(records)} rows in '{table}' ...")
    for i in range(0, len(records), 10):
        r = _request(method, url, headers, {"records": records[i:i + 10], "typecast": True})
        try:
            out.extend(r.json().get("records", []))
        except Exception:
            pass
        time.sleep(0.2)
        if i and i % 2000 == 0:
            print(f"  {i}/{len(records)}")
    return out


def upsert(rows, id_by_url, base, token, lib_table, full=False, existing=None):
    """Write harvested rows: link each to its source page, skip known documents (doc_id guard and
    purge tombstones), create the new ones and update changed ones. `existing` may be passed
    pre-filtered (e.g. one company, for scan_now.py). Returns (created_records, n_updated, n_blocked,
    known_docs), known_docs being every doc_id now held (discarded and tombstoned included)."""
    from ids import normalize
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

    if existing is None:
        existing = existing_library(base, token, lib_table)
    # Document-level guard. library_id is keyed on the URL, so a document served under a new URL
    # (per-visit token, cache-buster, mirror shard) used to come in as a new row on every run.
    # doc_id strips those parts (ids.doc_key); a harvested URL whose doc_id is already in the
    # library, discarded rows included, is not created again. dedupe_library.py catches what
    # this cannot see (same file under a genuinely different URL) after the files are attached.
    from ids import doc_id
    known_docs = {doc_id(v["wba"], v["url"]) for v in existing.values() if v.get("url")}
    # Documents deleted by purge_discarded.py are gone from the table but must not come back:
    # their tombstones (state/tombstones_report_library.tsv.gz) block them like a live row.
    from purge_discarded import load_tombstones
    tomb = load_tombstones()
    known_docs |= tomb
    print(f"doc_id guard: {len(known_docs):,} known documents ({len(tomb):,} from purge tombstones).")
    to_update = []; to_create = []; blocked = 0
    for r in rows:
        prev = existing.get(r["library_id"])
        if prev:
            r["first_seen"] = prev.get("first_seen") or TODAY_ISO   # keep original date
            if full or prev.get("url") != r["document_url"]:
                to_update.append({"id": prev["id"], "fields": r})
        else:
            did = doc_id(r["wba_id"], r["document_url"])
            if did in known_docs:
                blocked += 1
                continue
            known_docs.add(did)               # also stops two new URLs of one document in a run
            r["first_seen"] = TODAY_ISO
            to_create.append({"fields": r})
    print(f"{len(rows)} harvested; {len(existing)} already in '{lib_table}'. "
          f"writing {len(to_create)} new + {len(to_update)} changed (direct record writes); "
          f"{blocked} new URLs skipped as known documents (doc_id guard).")
    created = write_records(base, token, lib_table, to_create, "POST")
    write_records(base, token, lib_table, to_update, "PATCH")
    return created, len(to_update), blocked, known_docs


def page_health(records, types, known_docs, id_by_url, base, token, links_table):
    """Write, on each monitored page, how many of its document links are NOT in the library under
    any page of the company (unharvested_links) and up to 5 of them (unharvested_sample). Counts:
    harvestable links (PDF-shaped or probe-confirmed) whose document is not held, plus links on
    document-server addresses the probe has not yet ruled out. A shared PDF credited to another
    page of the company counts as held, so this is a real "the site offers it, we do not have it"
    signal, unlike counting library rows per page. Only changed rows are written."""
    import doc_probe
    from ids import doc_id, normalize
    cache = doc_probe.load_cache()
    updates = []; flagged = 0; done_ids = set()
    for f in records:
        if str(f.get(F_TYPE, "")).strip().lower() not in types:
            continue
        rid = id_by_url.get(normalize(f.get(F_URL, "")))
        # two page rows with the same address resolve to one record id; Airtable refuses a batch
        # that updates the same record twice (422), which once failed the whole harvest run
        if not rid or rid in done_ids:
            continue
        done_ids.add(rid)
        wba = f.get(F_WBA, "")
        missing = []
        for line in (f.get(F_SEEN) or "").split("\n"):
            u = fetchable(line)
            if not u:
                continue
            k = normalize(u)
            if is_pdf_url(u):
                target = (CONFIRMED_DOCS.get(k) if CONFIRMED_DOCS else None) or u
            elif doc_probe.priority(u) == 0 and cache.get(k, ("",))[0] != "not":
                target = u
            else:
                continue
            if doc_id(wba, target) not in known_docs:
                missing.append(u)
        n = len(missing); sample = "\n".join(missing[:5])
        if n >= 3:
            flagged += 1
        cur_n = f.get("unharvested_links")
        if cur_n != n or (f.get("unharvested_sample") or "") != sample:
            updates.append({"id": rid, "fields": {"unharvested_links": n, "unharvested_sample": sample}})
    print(f">>> page health: {flagged:,} pages with 3+ document links not in the library; "
          f"writing {len(updates):,} changed pages.", flush=True)
    write_records(base, token, links_table, updates, "PATCH")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["airtable", "csv"], default="airtable")
    ap.add_argument("--csv")
    ap.add_argument("--types", default=",".join(ALL_TYPES),
                    help="comma-separated page types to include")
    ap.add_argument("--reports-only", action="store_true")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--probe", action="store_true",
                    help="open links that do not look like files and harvest the ones serving a PDF "
                         "(doc_probe.py; results cached, capped per run)")
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

    if args.probe:
        import doc_probe
        links = (fetchable(l) for f in records if str(f.get(F_TYPE, "")).strip().lower() in types
                 for l in (f.get(F_SEEN) or "").split("\n") if l.strip())
        pdfs, summary = doc_probe.nightly(
            links, max_new=int(os.environ.get("PROBE_MAX_NEW") or 15000),
            budget_s=int(os.environ.get("PROBE_BUDGET_S") or 1500))
        CONFIRMED_DOCS.update(pdfs)
        print(">>> " + summary, flush=True)

    rows, per_company, per_type = build(records, types, args.reports_only)
    ok, bare = scheme_stats(records, types, args.reports_only)
    report(rows, per_company, per_type, ok, bare)

    if args.source == "airtable" and args.commit:
        from ids import normalize
        base = os.environ["AIRTABLE_BASE"]; token = os.environ["AIRTABLE_TOKEN"]
        lib_table = os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library")

        _, _, _, known_docs = upsert(rows, id_by_url, base, token, lib_table, full=args.full)
        page_health(records, types, known_docs, id_by_url, base, token,
                    os.environ.get("AIRTABLE_TABLE", "monitored_links"))
    elif args.source == "airtable":
        print("\nDRY-RUN. Re-run with --commit to write into the library table.")


if __name__ == "__main__":
    main()
