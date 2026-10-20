#!/usr/bin/env python3
"""
regulator_filings.py — annual reports from official regulator databases, for the gaps only.

About 1 in 5 companies sit behind bot walls we do not get around, and many more simply have no
2024+ annual report in the library. Listed companies must file their annual report with a
regulator, and those databases are open to automated access:

  SEC EDGAR (US)          10-K, 20-F, 40-F. The company's CIK is found once from its ISIN through
                          OpenFIGI (ISIN -> US ticker) and the SEC ticker list, and kept in
                          companies.sec_cik ('none' when it is not an SEC filer).
  ESEF (filings.xbrl.org) the European single electronic format annual financial report (EU, UK
                          and others), found by the company's LEI.

For each company and fiscal year from FIRST_YEAR on, a filing is added to report_library ONLY when
the library holds no annual-family document (Annual Report, Integrated Report, 10K Form) for that
year. So it fills gaps and never doubles a company's own annual report, which keeps the record
count low. Each added row is a real document with its official address, labelled at once (type,
year, auto_title) and marked file_status = not_pdf, because these filings are web documents
(HTML / XHTML), not PDFs: open them with document_url.

Identifiers (lei, isin, hq_country, ownership) come from WBA's company master list. Private and
government companies are skipped for the SEC. companies.filings_note says what was found.

Runs weekly (regulator_filings.yml). Dry run by default; --commit writes.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE, SEC_CONTACT (the contact the SEC asks for in the User-Agent).
Options: --wba ids, --source sec|esef|all, --max N
"""
import argparse, collections, hashlib, json, os, re, sys, time
from datetime import date
from urllib.parse import quote

import requests

API = "https://api.airtable.com/v0"
TOKEN = os.environ.get("AIRTABLE_TOKEN"); BASE = os.environ.get("AIRTABLE_BASE")
H = {"Authorization": f"Bearer {TOKEN}"}
FIRST_YEAR = 2023
ANNUAL_FAMILY = {"Annual Report", "Integrated Report", "10K Form"}
SEC_UA = {"User-Agent": f"World Benchmarking Alliance research {os.environ.get('SEC_CONTACT') or 'info@worldbenchmarkingalliance.org'}",
          "Accept-Encoding": "gzip, deflate"}
SEC_FORMS = {"10-K": "Form 10-K", "20-F": "Form 20-F", "40-F": "Form 40-F"}
ESEF = "https://filings.xbrl.org"


def at_get(table, fields, formula=None):
    from monitor_core import airtable_request
    params = [("pageSize", "100")] + [("fields[]", f) for f in fields] + \
             ([("filterByFormula", formula)] if formula else [])
    out = []; offset = None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", f"{API}/{BASE}/{quote(table)}", H, params=p).json()
        out += j.get("records", []); offset = j.get("offset")
        if not offset:
            return out
        time.sleep(0.12)


def at_write(table, method, records):
    from monitor_core import airtable_request
    out = []
    for i in range(0, len(records), 10):
        r = airtable_request(method, f"{API}/{BASE}/{quote(table)}", {**H, "Content-Type": "application/json"},
                             {"records": records[i:i + 10], "typecast": True})
        out += r.json().get("records", []); time.sleep(0.2)
    return out


def held(wbas=None):
    """{wba: set(years with an annual-family document)}, {wba: set(doc_ids)} from report_library."""
    from ids import doc_id
    years, docs = collections.defaultdict(set), collections.defaultdict(set)
    formula = None
    if wbas and len(wbas) <= 40:
        formula = "OR(" + ",".join(f"{{wba_id}}='{w}'" for w in wbas) + ")"
    for r in at_get("report_library", ["wba_id", "source_type_check", "source_type", "year", "discard",
                                       "document_url"], formula):
        f = r["fields"]; w = (f.get("wba_id") or "").strip()
        if not w:
            continue
        if f.get("document_url"):
            docs[w].add(doc_id(w, f["document_url"]))
        if f.get("discard"):
            continue
        t = (f.get("source_type_check") or "").strip() or (f.get("source_type") or "").strip()
        y = f.get("year")
        if t in ANNUAL_FAMILY and isinstance(y, (int, float)):
            years[w].add(int(y))
    return years, docs


# ---------------------------------------------------------------------------------------------- SEC
def sec_ciks(isins, sess):
    """{isin: cik or 'none'} via OpenFIGI (ISIN -> US ticker) and the SEC ticker list."""
    t = sess.get("https://www.sec.gov/files/company_tickers.json", headers=SEC_UA, timeout=60)
    t.raise_for_status()
    by_ticker = {v["ticker"].upper(): str(v["cik_str"]) for v in t.json().values()}
    out = {}
    isins = list(isins)
    for i in range(0, len(isins), 10):           # OpenFIGI without a key: 10 jobs/request, 25 req/min
        jobs = [{"idType": "ID_ISIN", "idValue": x, "exchCode": "US"} for x in isins[i:i + 10]]
        for attempt in range(4):
            r = sess.post("https://api.openfigi.com/v3/mapping", json=jobs, timeout=60)
            if r.status_code == 429:
                time.sleep(20); continue
            break
        res = r.json() if r.ok else [{}] * len(jobs)
        for job, item in zip(jobs, res):
            cik = "none"
            for d in (item or {}).get("data", []) or []:
                tick = (d.get("ticker") or "").upper().replace("/", "-").replace(" ", "-")
                if tick in by_ticker:
                    cik = by_ticker[tick]; break
            out[job["idValue"]] = cik
        time.sleep(2.6)
    return out


def sec_filings(cik, sess):
    """[(fiscal_year, form_label, url, filed)] newest first: original 10-K / 20-F / 40-F filings."""
    r = sess.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", headers=SEC_UA, timeout=60)
    time.sleep(0.15)                              # SEC fair access: well under 10 requests/second
    if not r.ok:
        return []
    rec = r.json().get("filings", {}).get("recent", {})
    out = []
    for form, acc, doc, rep, filed in zip(rec.get("form", []), rec.get("accessionNumber", []),
                                          rec.get("primaryDocument", []), rec.get("reportDate", []),
                                          rec.get("filingDate", [])):
        if form not in SEC_FORMS or not doc:
            continue
        y = int((rep or filed)[:4])
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"
        out.append((y, SEC_FORMS[form], url, filed))
    return out


# --------------------------------------------------------------------------------------------- ESEF
def esef_filings(lei, sess):
    """[(fiscal_year, label, url, published)] from filings.xbrl.org for this LEI."""
    flt = json.dumps([{"name": "entity.identifier", "op": "eq", "val": lei},
                      {"name": "period_end", "op": "ge", "val": f"{FIRST_YEAR}-01-01"}])
    r = sess.get(f"{ESEF}/api/filings", params={"filter": flt, "page[size]": "50", "sort": "-period_end"},
                 headers={"Accept": "application/vnd.api+json"}, timeout=60)
    time.sleep(0.3)
    if not r.ok:
        return []
    out = []
    for d in r.json().get("data", []):
        a = d.get("attributes", {})
        pe = a.get("period_end") or ""
        rel = a.get("report_url") or a.get("viewer_url") or ""
        if not (pe and rel):
            continue
        url = rel if rel.startswith("http") else ESEF + "/" + rel.lstrip("/")
        out.append((int(pe[:4]), "Annual financial report (ESEF)", url, a.get("date_added") or a.get("processed") or ""))
    return out


# --------------------------------------------------------------------------------------------- main
def rows_for(company, filings, have_years, have_docs, source):
    from ids import doc_id
    f = company["fields"]; wba = f["wba_id"]; name = f.get("company_name", "")
    best = {}
    for y, label, url, filed in filings:          # one filing per fiscal year: the first (newest)
        if y >= FIRST_YEAR and y not in best:
            best[y] = (label, url, filed)
    rows, notes = [], []
    for y in sorted(best):
        label, url, filed = best[y]
        if y in have_years:
            continue
        if doc_id(wba, url) in have_docs:
            continue
        key = url.strip().lower()
        rows.append({"library_id": f"{wba}-{hashlib.sha1(key.encode()).hexdigest()[:10]}", "wba_id": wba,
                     "company_name": name, "document_url": url, "doc_year": str(y),
                     "source_type": "Annual Report", "match_confidence": f"regulator filing ({source})",
                     "auto_title": f"{label} for fiscal year {y}", "file_status": "not_pdf",
                     "first_seen": date.today().isoformat(),
                     "Reviewer notes": f"Added from the {source} regulator database (filed {filed[:10]}). "
                                       "A web document, not a PDF: open document_url."})
        notes.append(f"{label} {y}")
    return rows, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--wba", default="")
    ap.add_argument("--source", default="all", choices=["sec", "esef", "all"])
    ap.add_argument("--max", type=int, default=0)
    a = ap.parse_args()
    if not (TOKEN and BASE):
        sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
    wbas = [w.strip() for w in a.wba.split(",") if w.strip()]
    formula = ("OR(" + ",".join(f"{{wba_id}}='{w}'" for w in wbas) + ")") if wbas else None
    comps = [c for c in at_get("companies", ["wba_id", "company_name", "lei", "isin", "hq_country", "ownership",
                                             "sec_cik"], formula) if c["fields"].get("wba_id")]
    if a.max:
        comps = comps[:a.max]
    print(f">>> {len(comps)} companies; reading what the library holds ...", flush=True)
    have_years, have_docs = held(wbas)
    sess = requests.Session()
    stats = collections.Counter(); new_rows = []; comp_upd = {}

    if a.source in ("sec", "all"):
        need = {c["fields"]["isin"] for c in comps if c["fields"].get("isin") and not c["fields"].get("sec_cik")
                and c["fields"].get("ownership") == "Public"
                and (c["fields"]["isin"].startswith("US") or c["fields"].get("hq_country") in ("USA", "CAN"))}
        if need:
            print(f">>> SEC: looking up {len(need)} ISINs ...", flush=True)
            found = sec_ciks(need, sess)
            for c in comps:
                i = c["fields"].get("isin")
                if i in found:
                    c["fields"]["sec_cik"] = found[i]
                    comp_upd.setdefault(c["id"], {})["sec_cik"] = found[i]
        for c in comps:
            cik = c["fields"].get("sec_cik")
            if not cik or cik == "none":
                continue
            w = c["fields"]["wba_id"]
            rows, notes = rows_for(c, sec_filings(cik, sess), have_years[w], have_docs[w], "SEC")
            stats["sec companies"] += 1; stats["sec filings added"] += len(rows)
            new_rows += rows
            if notes:
                comp_upd.setdefault(c["id"], {}).setdefault("_notes", []).append("SEC: " + ", ".join(notes))
                have_years[w] |= {int(r["doc_year"]) for r in rows}

    if a.source in ("esef", "all"):
        for c in comps:
            lei = (c["fields"].get("lei") or "").strip()
            if not lei:
                continue
            w = c["fields"]["wba_id"]
            fl = esef_filings(lei, sess)
            stats["esef companies"] += 1
            if fl:
                stats["esef companies with filings"] += 1
            rows, notes = rows_for(c, fl, have_years[w], have_docs[w], "ESEF")
            stats["esef filings added"] += len(rows)
            new_rows += rows
            if notes:
                comp_upd.setdefault(c["id"], {}).setdefault("_notes", []).append("ESEF: " + ", ".join(notes))
                have_years[w] |= {int(r["doc_year"]) for r in rows}

    print(f">>> {dict(stats)}; {len(new_rows)} annual filings to add.", flush=True)
    for r in new_rows[:8]:
        print(f"    {r['wba_id']} {r['doc_year']} {r['auto_title']} {r['document_url']}", flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(f"## Regulator filings\n\n{dict(stats)}\n\n{len(new_rows)} annual filings "
                     f"{'added' if a.commit else 'would be added (dry run)'}.\n")
    if not a.commit:
        print(">>> dry run, nothing written."); return
    created = at_write("report_library", "POST", [{"fields": r} for r in new_rows])
    print(f">>> added {len(created)} filings to report_library.", flush=True)
    today = date.today().isoformat()
    upd = []
    for cid, f in comp_upd.items():
        fields = {k: v for k, v in f.items() if not k.startswith("_")}
        if f.get("_notes"):
            fields["filings_note"] = f"{today}: added " + "; ".join(f["_notes"])
        if fields:
            upd.append({"id": cid, "fields": fields})
    at_write("companies", "PATCH", upd)
    touched = sorted({r["wba_id"] for r in new_rows})
    import subprocess
    for i in range(0, len(touched), 40):
        subprocess.run([sys.executable, "company_coverage.py", "--commit", "--wba", ",".join(touched[i:i + 40])],
                       check=False)


if __name__ == "__main__":
    main()
