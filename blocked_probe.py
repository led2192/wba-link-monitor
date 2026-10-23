#!/usr/bin/env python3
"""
blocked_probe.py — when a company's site refuses our robot, which free route still gets the report?

Read-only: writes nothing to Airtable. It takes what the pipeline cannot reach today:
  documents  report_library rows with no file and file_status = blocked (report families first,
             2023+ first), up to --docs
  pages      monitored report pages (reports hub, sustainability, investor relations, policies) whose
             last read was refused (401/403/429), up to --pages
and tries, for each, one route after another, recording which ones work:
  plain      the robot's own request again (is it still refused today?)
  headers    a full browser-like request: every header a browser sends, the company's own page as
             Referer, cookies from a first visit to the home page
  browser    headless Chromium (Playwright), the same engine as the browser lane
  archive    the Internet Archive's copy (Wayback Machine): for a document, a stored PDF; for a page,
             a stored copy and how many PDF links it lists
For every refused item it also records the company's country and listing, to size a fourth route:
reports filed with a stock exchange (HKEX, SGX, JPX, NSE/BSE, ASX, KRX, TWSE...), as the SEC and
ESEF routes already do for the US and Europe. That one is counted, not tried.

One request at a time per site, with pauses, so no site sees more than a trickle.
Output: blocked_probe.md (summary, posted on the PR by the workflow) and blocked_probe.csv (one row
per item and route, kept as a workflow artifact; it names companies, so it is not committed).
Env: AIRTABLE_TOKEN, AIRTABLE_BASE.  Args: --docs 300 --pages 300 --no-browser
"""
import argparse, collections, csv, os, random, re, threading, time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlsplit

import requests

from monitor_core import airtable_request

API = "https://api.airtable.com/v0"
TOKEN, BASE = os.environ.get("AIRTABLE_TOKEN"), os.environ.get("AIRTABLE_BASE")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
PLAIN = {"User-Agent": UA}
BROWSER_HEADERS = {
    "User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip, deflate, br", "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "same-origin", "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"'}
REFUSED = {401, 403, 429}
FAMILY = {"Sustainability Report", "ESG Report", "CSR Report", "Integrated Report", "Impact Report",
          "Annual Report", "10K Form"}
PAGE_TYPES = ("reports_hub", "sustainability_page", "investor_relations", "policies")
# countries whose main exchange publishes listed companies' annual (and often ESG) reports as PDFs
EXCHANGES = {"HKG": "HKEX", "CHN": "HKEX/SSE/SZSE", "SGP": "SGX", "JPN": "JPX/TDnet", "IND": "NSE/BSE",
             "AUS": "ASX", "KOR": "KRX/DART", "TWN": "TWSE/MOPS", "MYS": "Bursa", "THA": "SET",
             "IDN": "IDX", "PHL": "PSE", "ZAF": "JSE", "BRA": "B3/CVM", "MEX": "BMV", "CHL": "CMF",
             "SAU": "Tadawul", "ARE": "ADX/DFM", "NZL": "NZX", "VNM": "HOSE"}
PDF_LINK = re.compile(rb"""href=["']([^"']+?\.pdf[^"']*)["']""", re.I)

_site_locks = collections.defaultdict(threading.Lock)


def polite(url):
    """One request at a time per site, with a pause."""
    lock = _site_locks[urlsplit(url).netloc.lower()]
    lock.acquire()
    return lock


def release(lock):
    time.sleep(1.5 + random.random())
    lock.release()


def get(table, fields, formula):
    H = {"Authorization": f"Bearer {TOKEN}"}
    params = [("pageSize", "100"), ("filterByFormula", formula)] + [("fields[]", f) for f in fields]
    out, offset = [], None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", f"{API}/{BASE}/{quote(table)}", H, params=p).json()
        for r in j.get("records", []):
            f = r.get("fields", {}); f["_id"] = r["id"]; out.append(f)
        offset = j.get("offset")
        if not offset:
            return out
        time.sleep(0.15)


def fetch(url, headers, sess=None, referer=None):
    """(code, is_pdf, n_pdf_links, error) for one request."""
    s = sess or requests
    h = dict(headers, **({"Referer": referer} if referer else {}))
    lock = polite(url)
    try:
        r = s.get(url, headers=h, timeout=30, allow_redirects=True, stream=True)
        body = b""
        for chunk in r.iter_content(65536):
            body += chunk
            if len(body) > 2_000_000:
                break
        r.close()
        return r.status_code, body[:5].startswith(b"%PDF"), len(set(PDF_LINK.findall(body))), ""
    except requests.RequestException as e:
        return None, False, 0, type(e).__name__
    finally:
        release(lock)


def try_plain(item):
    code, pdf, links, err = fetch(item["url"], PLAIN)
    return ok(item, code, pdf, links), f"{code or err}"


def try_headers(item):
    sess = requests.Session()
    home = f"{urlsplit(item['url']).scheme}://{urlsplit(item['url']).netloc}/"
    fetch(home, BROWSER_HEADERS, sess)                       # cookies, as a person arriving would
    code, pdf, links, err = fetch(item["url"], BROWSER_HEADERS, sess, referer=item.get("referer") or home)
    return ok(item, code, pdf, links), f"{code or err}" + (f", {links} pdf links" if item["kind"] == "page" else "")


def try_archive(item):
    """The Wayback Machine's latest copy: for a document a real PDF, for a page a copy with PDF links."""
    lock = polite("https://archive.org/")
    try:
        j = requests.get("https://archive.org/wayback/available", params={"url": item["url"]},
                         headers=PLAIN, timeout=30).json()
    except Exception as e:
        return False, f"lookup failed ({type(e).__name__})"
    finally:
        release(lock)
    snap = (j.get("archived_snapshots") or {}).get("closest") or {}
    if not snap.get("available"):
        return False, "no copy"
    ts = snap.get("timestamp", "")
    raw = f"https://web.archive.org/web/{ts}id_/{item['url']}"     # id_ = the original bytes
    code, pdf, links, err = fetch(raw, PLAIN)
    return ok(item, code, pdf, links), f"copy of {ts[:8]}: {code or err}" + (f", {links} pdf links" if item["kind"] == "page" else "")


def ok(item, code, pdf, links):
    if code is None or code >= 400:
        return False
    return pdf if item["kind"] == "doc" else links > 0


def browser_pass(items):
    """Headless Chromium on every item still not reached by the cheaper routes."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {it["id"]: (False, "playwright not installed") for it in items}
    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(user_agent=UA, accept_downloads=True, locale="en-US")
        for it in items:
            lock = polite(it["url"])
            try:
                if it["kind"] == "doc":
                    r = ctx.request.get(it["url"], timeout=45000, headers={"Referer": it.get("referer") or ""})
                    body = r.body()[:5]
                    out[it["id"]] = (r.ok and body.startswith(b"%PDF"), f"{r.status}")
                else:
                    page = ctx.new_page()
                    resp = page.goto(it["url"], timeout=45000, wait_until="domcontentloaded")
                    page.wait_for_timeout(3000)
                    hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
                    n = len({h for h in hrefs if ".pdf" in h.lower()})
                    code = resp.status if resp else None
                    out[it["id"]] = (bool(code and code < 400 and n), f"{code}, {n} pdf links")
                    page.close()
            except Exception as e:
                out[it["id"]] = (False, type(e).__name__)
            finally:
                release(lock)
        b.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", type=int, default=300)
    ap.add_argument("--pages", type=int, default=300)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    comps = {c.get("wba_id"): c for c in get("companies", ["wba_id", "company_name", "hq_country", "ownership"], "{wba_id}!=''")}

    docs = get("report_library", ["wba_id", "document_url", "found_on", "effective_type", "year"],
               "AND(NOT({discard}), {file}='', {file_status}='blocked', {document_url}!='')")
    docs.sort(key=lambda d: (d.get("effective_type") not in FAMILY, -(d.get("year") or 0)))
    pages = get("monitored_links", ["wba_id", "url", "type", "http_status", "status"],
                "AND({monitor}, OR({status}='blocked', AND({status}='dead', OR({http_status}=401, {http_status}=403, {http_status}=429))))")
    pages = [p for p in pages if (p.get("type") or "") in PAGE_TYPES]
    random.Random(7).shuffle(pages)
    items = [{"id": d["_id"], "kind": "doc", "url": d["document_url"], "referer": d.get("found_on"),
              "wba": d.get("wba_id"), "what": f"{d.get('effective_type') or 'untyped'} {d.get('year') or ''}".strip(),
              "family_recent": d.get("effective_type") in FAMILY and (d.get("year") or 0) >= 2023}
             for d in docs[:a.docs]]
    items += [{"id": p["_id"], "kind": "page", "url": p["url"], "wba": p.get("wba_id"), "what": p.get("type"),
               "family_recent": False} for p in pages[:a.pages]]
    print(f">>> {len(docs):,} refused documents ({len(items) - min(len(pages), a.pages)} probed); "
          f"{len(pages):,} refused report pages ({min(len(pages), a.pages)} probed).", flush=True)

    res = collections.defaultdict(dict)

    def run(it):
        for name, fn in (("plain", try_plain), ("headers", try_headers), ("archive", try_archive)):
            res[it["id"]][name] = fn(it)
        return it
    with ThreadPoolExecutor(max_workers=12) as ex:
        for i, _ in enumerate(ex.map(run, items), 1):
            if i % 50 == 0:
                print(f"  {i}/{len(items)}", flush=True)
    if not a.no_browser:
        todo = [it for it in items if not any(res[it["id"]][m][0] for m in ("plain", "headers"))]
        print(f">>> browser on {len(todo)} items not reached by a plain or full request", flush=True)
        for iid, r in browser_pass(todo).items():
            res[iid]["browser"] = r

    routes = ["plain", "headers", "browser", "archive"]
    with open("blocked_probe.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["kind", "wba_id", "company", "country", "what", "url"] + routes)
        for it in items:
            c = comps.get(it["wba"]) or {}
            w.writerow([it["kind"], it["wba"], c.get("company_name", ""), c.get("hq_country", ""), it["what"], it["url"]]
                       + [("OK " if res[it["id"]].get(m, (False, ""))[0] else "no ") + str(res[it["id"]].get(m, (False, "not tried"))[1])
                          for m in routes])

    def table(kind, cond=lambda it: True):
        sel = [it for it in items if it["kind"] == kind and cond(it)]
        n = len(sel) or 1
        any_free = sum(1 for it in sel if any(res[it["id"]].get(m, (False,))[0] for m in routes))
        rows = [f"| {m} | {sum(1 for it in sel if res[it['id']].get(m, (False,))[0]):,} | "
                f"{sum(1 for it in sel if res[it['id']].get(m, (False,))[0]) / n:.0%} |" for m in routes]
        return len(sel), any_free, rows

    def exchange_share(kind):
        sel = [it for it in items if it["kind"] == kind and not any(res[it["id"]].get(m, (False,))[0] for m in routes)]
        by = collections.Counter()
        for it in sel:
            c = comps.get(it["wba"]) or {}
            if (c.get("hq_country") in EXCHANGES) and "public" in (c.get("ownership") or "").lower():
                by[EXCHANGES[c["hq_country"]]] += 1
        return len(sel), by

    L = ["## Refused by the company's site: which free route still works?", "",
         "Read-only probe. Each item is tried with the robot's own request again, a full browser-like request, "
         "headless Chromium, and the Internet Archive's copy. A document counts as reached when a real PDF comes back; "
         "a page when it opens and lists PDF links.", ""]
    for kind, label in (("doc", "Documents without their PDF (file_status = blocked)"), ("page", "Report pages refused (401/403/429)")):
        n, anyf, rows = table(kind)
        L += [f"### {label}: {n:,} probed", "", "| route | reached | share |", "|---|---:|---:|"] + rows
        L += ["", f"**Reached by at least one free route: {anyf:,} of {n:,} ({anyf / (n or 1):.0%}).**", ""]
        if kind == "doc":
            n2, any2, _ = table("doc", lambda it: it["family_recent"])
            L += [f"Of these, sustainability or annual reports of 2023+: {n2:,}, reached {any2:,}.", ""]
        left, by = exchange_share(kind)
        L += [f"Still unreached: {left:,}. Of those, listed companies in a country whose exchange publishes reports: "
              f"{sum(by.values()):,}" + (" (" + ", ".join(f"{k} {v}" for k, v in by.most_common()) + ")" if by else "") + ".", ""]
    doms = collections.Counter(urlsplit(it["url"]).netloc for it in items
                               if not any(res[it["id"]].get(m, (False,))[0] for m in routes))
    L += ["### Sites still refusing every free route (top 25)", "", "| site | items |", "|---|---:|"]
    L += [f"| {d} | {n} |" for d, n in doms.most_common(25)]
    out = "\n".join(L)
    print(out, flush=True)
    open("blocked_probe.md", "w").write(out + "\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(out + "\n")


if __name__ == "__main__":
    main()
