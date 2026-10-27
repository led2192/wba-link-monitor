#!/usr/bin/env python3
"""
wayback_backfill.py: reports the Internet Archive kept that the company's site no longer offers.

The Wayback Machine has captured most company sites for twenty years, and its index (the CDX API,
free, no key) lists every PDF ever captured under a domain with the day of the first capture. For a
company whose record has gaps that is a second source the robot cannot reach any other way: reports
the company removed from its site, years before the robot started watching, and sites that block it
(the archive's copy is fetched from archive.org, not from the company). Owner's idea of 27 Oct 2026,
after finding Apax Partners' sustainability reports Edition 2 to 8 there while the library held 2016,
2019 and 2023.

For each company with a gap (default: no key report in the latest or the previous wave, see
key_reports.py; --all for every company; --wba for a list):
  1. the domains of its watched pages (monitored_links), minus shared hosts (CDNs, asset managers);
  2. every PDF captured under those domains, earliest capture first (one row per address);
  3. keep the ones whose address names a report of the key families (REPORT_TYPES, by the same URL
     classifier the harvester uses), of at least MIN_KB, and not already in the library under the
     original address or the archived one (discarded rows and purge tombstones included);
  4. propose a report_library row whose document_url is the archived copy
     (https://web.archive.org/web/<timestamp>id_/<original>: the file itself, no archive banner),
     typed and dated from the address, with Reviewer notes saying where it came from and when it was
     first captured. The capture day is an upper bound on the publication date (pub_dates.py reads
     "(captured YYYY-MM-DD)" from the notes).

The attachment, cover, classification, year and completeness jobs then treat the row like any other.
At most MAX_PER_COMPANY rows per company and run, oldest reports first, so one company cannot flood a
run; a later run adds the rest.

Dry run by default: prints and summarises what would be added. --commit writes the rows and refreshes
the companies' coverage. Env: AIRTABLE_TOKEN, AIRTABLE_BASE. Options: --all, --wba ids, --max N
companies, --max-rows N, --since YYYY (captures from that year, default 2005).
"""
import argparse, collections, hashlib, json, os, re, sys, time
from datetime import date
from urllib.parse import quote, urlsplit

API = "https://api.airtable.com/v0"
CDX = "https://web.archive.org/cdx/search/cdx"
UA = {"User-Agent": "wba-link-monitor (World Benchmarking Alliance research; info@worldbenchmarkingalliance.org)"}
REPORT_TYPES = {"Annual Report", "Integrated Report", "Sustainability Report", "ESG Report", "CSR Report",
                "Impact Report"}
MIN_KB = 150                      # a report is bigger than a certificate or a press release
MAX_PER_COMPANY = 40
MAX_DOMAINS = 4
SHARED_HOSTS = ("cloudfront.net", "amazonaws.com", "q4cdn.com", "q4web.com", "windows.net", "googleapis.com",
                "akamaized.net", "akamaihd.net", "azureedge.net", "cloudinary.com", "box.com", "dropbox.com",
                "sharepoint.com", "google.com", "youtube.com", "linkedin.com", "sec.gov", "xbrl.org",
                "issuu.com", "yumpu.com", "globenewswire.com", "prnewswire.com", "businesswire.com")
PAUSE = 1.0                       # the archive asks for about one request a second
CAPTURED = re.compile(r"\(captured (20\d\d-\d\d-\d\d)\)")


# ---------------------------------------------------------------------------------------------- Airtable
def at_get(table, fields, formula=None):
    from monitor_core import airtable_request
    token, base = os.environ["AIRTABLE_TOKEN"], os.environ["AIRTABLE_BASE"]
    h = {"Authorization": f"Bearer {token}"}
    params = [("pageSize", "100")] + [("fields[]", f) for f in fields] + \
             ([("filterByFormula", formula)] if formula else [])
    out, offset = [], None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", f"{API}/{base}/{quote(table)}", h, params=p).json()
        out += j.get("records", []); offset = j.get("offset")
        if not offset:
            return out
        time.sleep(0.12)


def at_write(table, method, records):
    from monitor_core import airtable_request
    token, base = os.environ["AIRTABLE_TOKEN"], os.environ["AIRTABLE_BASE"]
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    out = []
    for i in range(0, len(records), 10):
        r = airtable_request(method, f"{API}/{base}/{quote(table)}", h, {"records": records[i:i + 10], "typecast": True})
        out += r.json().get("records", []); time.sleep(0.2)
    return out


def gap_companies(wbas, everything):
    """[(record id, wba_id, company_name)] of the companies to look at."""
    if wbas:
        formula = "OR(" + ",".join(f"{{wba_id}}='{w}'" for w in wbas) + ")"
    elif everything:
        formula = None
    else:
        formula = "OR({kr_latest_status}='None', {kr_prev_status}='None', {kr_latest_status}='', {kr_prev_status}='')"
    rows = at_get("companies", ["wba_id", "company_name", "kr_latest_status", "kr_prev_status"], formula)
    return [(r["id"], r["fields"]["wba_id"], r["fields"].get("company_name", "")) for r in rows if r["fields"].get("wba_id")]


def company_domains(wbas):
    """{wba_id: [registered domains of its watched pages]}, shared hosts left out, at most MAX_DOMAINS."""
    from monitor_core import reg_domain, is_dam_host, is_framework_host
    formula = None
    if len(wbas) <= 40:
        formula = "OR(" + ",".join(f"{{wba_id}}='{w}'" for w in wbas) + ")"
    counts = collections.defaultdict(collections.Counter)
    for r in at_get("monitored_links", ["wba_id", "url"], formula):
        f = r["fields"]; w = (f.get("wba_id") or "").strip(); u = f.get("url") or ""
        if w not in wbas or not u:
            continue
        host = (urlsplit(u).hostname or "").lower()
        d = reg_domain(u)
        if not d or shared_host(host) or is_dam_host(host) or is_framework_host(host):
            continue
        counts[w][d] += 1
    return {w: [d for d, _ in c.most_common(MAX_DOMAINS)] for w, c in counts.items()}


def shared_host(host):
    host = (host or "").lower()
    return any(host == s or host.endswith("." + s) for s in SHARED_HOSTS)


def held_docs(wbas):
    """{wba_id: set(doc_id)} of every document the library holds (discarded included) plus the purge
    tombstones, so nothing comes back twice."""
    from ids import doc_id
    from purge_discarded import load_tombstones
    formula = None
    if len(wbas) <= 40:
        formula = "OR(" + ",".join(f"{{wba_id}}='{w}'" for w in wbas) + ")"
    docs = collections.defaultdict(set)
    for r in at_get("report_library", ["wba_id", "document_url", "Reviewer notes"], formula):
        f = r["fields"]; w = (f.get("wba_id") or "").strip()
        if not w or w not in wbas:
            continue
        if f.get("document_url"):
            docs[w].add(doc_id(w, f["document_url"]))
            orig = original_of(f["document_url"])
            if orig:
                docs[w].add(doc_id(w, orig))
        m = re.search(r"Original address: (\S+)", f.get("Reviewer notes") or "")
        if m:
            docs[w].add(doc_id(w, m.group(1)))
    for t in load_tombstones():
        w = t.split("|", 1)[0]
        if w in wbas:
            docs[w].add(t)
    return docs


# ---------------------------------------------------------------------------------------------- the archive
def cdx_rows(domain, sess, since=2005, limit=3000):
    """[(timestamp, original url, length)] of the PDFs captured under a domain and its subdomains,
    one row per address (its earliest capture that answered 200). [] when the archive does not answer."""
    params = {"url": domain, "matchType": "domain", "filter": ["mimetype:application/pdf", "statuscode:200"],
              "collapse": "urlkey", "fl": "timestamp,original,length", "output": "json", "from": str(since),
              "limit": str(limit)}
    for attempt in range(4):
        try:
            r = sess.get(CDX, params=params, headers=UA, timeout=120)
        except Exception:
            time.sleep(10 * (attempt + 1)); continue
        if r.status_code == 200:
            try:
                rows = r.json()
            except ValueError:
                return []
            return [(t, u, int(n) if str(n).isdigit() else 0) for t, u, n in rows[1:]] if rows else []
        if r.status_code in (429, 503, 504):
            time.sleep(30 * (attempt + 1)); continue
        return []
    return []


def archive_url(timestamp, original):
    """The archived file itself (id_ = no archive banner, the bytes as captured)."""
    return f"https://web.archive.org/web/{timestamp}id_/{original}"


def original_of(url):
    m = re.match(r"https?://web\.archive\.org/web/\d{4,14}(?:id_)?/(https?://.+)$", url or "")
    return m.group(1) if m else None


def capture_date(timestamp):
    return date(int(timestamp[:4]), int(timestamp[4:6]), int(timestamp[6:8]))


def candidates(wba, name, rows, have, this_year=None):
    """report_library rows to add for one company from its CDX rows: report-family addresses, big
    enough, not held; oldest capture first; at most MAX_PER_COMPANY."""
    from classify_library import classify, year_from
    from ids import doc_id
    out, seen = [], set()
    for ts, orig, length in sorted(rows):
        if not orig.lower().startswith(("http://", "https://")):
            continue
        if length and length < MIN_KB * 1024:
            continue
        typ, conf = classify(orig)
        if typ not in REPORT_TYPES:
            continue
        arch = archive_url(ts, orig)
        ids = {doc_id(wba, orig), doc_id(wba, arch)}
        if ids & have or ids & seen:
            continue
        seen |= ids
        fname = urlsplit(orig).path.rsplit("/", 1)[-1]
        cap = capture_date(ts)
        year = year_from(fname, "", this_year)
        if year and int(year) > cap.year:
            year = ""                                  # a file name year after the capture is not the report's year
        row = {"library_id": f"{wba}-{hashlib.sha1(arch.strip().lower().encode()).hexdigest()[:10]}",
               "wba_id": wba, "company_name": name, "document_url": arch, "source_type": typ,
               "match_confidence": f"wayback ({conf})", "first_seen": date.today().isoformat(),
               "Reviewer notes": f"Added from the Wayback Machine (captured {cap.isoformat()}). "
                                 f"Original address: {orig}"}
        if year:
            row["doc_year"] = year
        out.append(row)
        if len(out) >= MAX_PER_COMPANY:
            break
    return out


# ---------------------------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--all", action="store_true", help="every company, not only the ones with a key report gap")
    ap.add_argument("--wba", default="", help="comma-separated wba_ids")
    ap.add_argument("--max", type=int, default=0, help="companies this run (0 = all selected)")
    ap.add_argument("--max-rows", type=int, default=0, help="stop after this many rows (0 = no cap)")
    ap.add_argument("--since", type=int, default=2005)
    a = ap.parse_args()
    if not (os.environ.get("AIRTABLE_TOKEN") and os.environ.get("AIRTABLE_BASE")):
        sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
    import requests
    import qa
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("wayback_backfill.py")
    wbas = [w.strip() for w in a.wba.split(",") if w.strip()]
    comps = gap_companies(wbas, a.all)
    if a.max:
        comps = comps[:a.max]
    sel = {w for _, w, _ in comps}
    print(f">>> {len(comps)} companies ({'listed' if wbas else 'all' if a.all else 'with a key report gap'}); "
          f"reading their pages and documents ...", flush=True)
    domains = company_domains(sel)
    have = held_docs(sel)
    sess = requests.Session()
    stats = collections.Counter(); new_rows = []; per_company = []
    for rec_id, w, name in comps:
        ds = domains.get(w) or []
        if not ds:
            stats["companies without a domain"] += 1
            continue
        rows = []
        for d in ds:
            got = cdx_rows(d, sess, a.since)
            rows += got
            stats["archive requests"] += 1
            if not got:
                stats["domains with nothing"] += 1
            time.sleep(PAUSE)
        cand = candidates(w, name, rows, have[w])
        stats["companies looked at"] += 1
        stats["archived pdfs seen"] += len(rows)
        if cand:
            stats["companies with something to add"] += 1
            years = sorted({r.get("doc_year", "?") for r in cand})
            per_company.append((w, name, len(cand), ", ".join(ds), " ".join(years)))
            print(f"    {w} {name}: {len(cand)} from {', '.join(ds)} (years {' '.join(years)})", flush=True)
        new_rows += cand
        if a.max_rows and len(new_rows) >= a.max_rows:
            new_rows = new_rows[:a.max_rows]
            stats["stopped at --max-rows"] = 1
            break
    by_type = collections.Counter(r["source_type"] for r in new_rows)
    by_year = collections.Counter(r.get("doc_year", "undated") for r in new_rows)
    verb = "added" if a.commit else "would be added (dry run)"
    md = [f"### Wayback Machine backfill{'' if a.commit else ' (dry run)'}", "",
          f"{stats['companies looked at']:,} companies looked at, {stats['companies with something to add']:,} with "
          f"reports to add; {len(new_rows):,} rows {verb}. "
          f"Archived PDFs seen: {stats['archived pdfs seen']:,}; companies without a usable domain: "
          f"{stats['companies without a domain']:,}.", "",
          "By type: " + ", ".join(f"{t} {n:,}" for t, n in by_type.most_common()), "",
          "By year: " + ", ".join(f"{y} {n:,}" for y, n in sorted(by_year.items(), key=lambda kv: str(kv[0]))), ""]
    if per_company:
        md += ["| Company | Rows | Domains | Years |", "| --- | --: | --- | --- |"]
        md += [f"| {w} {n} | {k} | {d} | {y} |" for w, n, k, d, y in per_company[:60]]
        if len(per_company) > 60:
            md.append(f"| ... and {len(per_company) - 60} more | | | |")
        md.append("")
    open("wayback_backfill.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    for r in new_rows[:10]:
        print(f"    {r['wba_id']} {r.get('doc_year', '----')} {r['source_type']}: {r['document_url']}", flush=True)
    if not a.commit:
        print(">>> dry run, nothing written.", flush=True)
        run.finish("ok", rows_in=len(comps), rows_out=0, counters=dict(stats))
        return
    created = at_write("report_library", "POST", [{"fields": r} for r in new_rows])
    print(f">>> added {len(created)} rows to report_library.", flush=True)
    touched = sorted({r["wba_id"] for r in new_rows})
    import subprocess
    for i in range(0, len(touched), 40):
        subprocess.run([sys.executable, "company_coverage.py", "--commit", "--wba", ",".join(touched[i:i + 40])],
                       check=False)
    run.finish("ok", rows_in=len(comps), rows_out=len(created), counters=dict(stats))


if __name__ == "__main__":
    main()
