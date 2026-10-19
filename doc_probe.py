#!/usr/bin/env python3
"""
doc_probe.py — find documents hiding behind addresses that do not look like documents.

The harvester only accepts links that LOOK like files (.pdf, or a known document endpoint such as
Q4 /static-files/ or /files/<slug>). Many corporate sites serve PDFs from addresses with no .pdf at
all: Sitecore /-/media/....ashx, /document/<slug>, /attachment/download/<id>, media?mediaId=...,
/download/18265. In October 2026 a 10% sample of monitored pages held ~1,700 such links on
document-server paths across ~290 companies, none of them harvested (Aldi Sud: 13 reports for
months). New schemes appear all the time, so instead of adding one rule per site this module OPENS
the link (first 1 KB) and asks the server: is this a PDF?

Every answer is remembered in a cache (one line per link: key, verdict, date), so each link is
checked once, ever. harvest_reports.py runs it nightly with a per-night cap and time budget,
document-server paths first, then other report-ish extensionless links, and treats every link with
verdict 'pdf' as a document. scan_now.py uses probe() directly on the page it scans.

Cache: PROBE_CACHE (default state/probe_cache.tsv.gz). The nightly workflow keeps it between runs
with actions/cache, so nothing is committed to the repository. Losing it only means re-checking.
"""
import gzip, os, re, time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from urllib.parse import urlsplit

from ids import normalize

CACHE = os.environ.get("PROBE_CACHE", "state/probe_cache.tsv.gz")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
# Never documents we want as PDFs: web pages, spreadsheets, archives, media.
NOT_PDF_EXT = re.compile(r"\.(html?|aspx?|php|jsp|do|cshtml|page|xlsx?|docx?|pptx?|zip|csv|mp[34]|"
                         r"jpe?g|png|gif|svg|webp|xml|json|txt|ics)$", re.I)
SERVER = re.compile(r"download|/documents?/|/files?/|attachment|getmedia|/media/|/blob/|/dam/|"
                    r"/assets?/|/uploads?/|/docs?/|fileadmin|/pdfs?/|/resources?/|\.ashx$|/api/", re.I)
WORDY = re.compile(r"report|annual|sustainab|esg|polic|statement|disclosure|charter|code-of|"
                   r"modern-slavery|tcfd|cdp|gri", re.I)


def priority(url):
    """0 = document-server path (check first), 1 = report-ish extensionless name, None = skip."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    if path.lower().endswith(".pdf") or NOT_PDF_EXT.search(path):
        return None
    if SERVER.search(path):
        return 0
    last = ([s for s in path.split("/") if s] or [""])[-1]
    if "." not in last and WORDY.search(last):
        return 1
    return None


def is_pdf(url, session=None, timeout=20):
    """'pdf', 'not' or 'err'. Reads at most the first 1 KB."""
    import requests
    s = session or requests
    try:
        r = s.get(url, headers={**UA, "Range": "bytes=0-1023"}, timeout=timeout, stream=True,
                  allow_redirects=True)
        head = next(r.iter_content(1024), b"")
        ctype = (r.headers.get("Content-Type") or "").lower()
        code = r.status_code
        r.close()
    except Exception:
        return "err"
    if code >= 400:
        return "err"
    return "pdf" if ("pdf" in ctype or head.startswith(b"%PDF")) else "not"


def probe(urls, workers=12, budget_s=None, session=None):
    """{normalized_key: verdict} for these URLs, checked in parallel, stopping at the time budget."""
    out = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {}
        for u in urls:
            futs[ex.submit(is_pdf, u, session)] = u
        for f, u in futs.items():
            if budget_s and time.time() - t0 > budget_s:
                f.cancel()
                continue
            try:
                out[normalize(u)] = f.result()
            except Exception:
                out[normalize(u)] = "err"
    return out


def load_cache(path=CACHE):
    cache = {}
    if os.path.exists(path):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 2:
                    cache[parts[0]] = (parts[1], parts[2] if len(parts) > 2 else "")
    return cache


def save_cache(cache, path=CACHE):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for k, (v, d) in cache.items():
            fh.write(f"{k}\t{v}\t{d}\n")


def nightly(links, max_new=15000, budget_s=1500, retry_err_days=30):
    """links: iterable of fetchable URLs from seen_links. Checks the ones never checked (and errors
    older than retry_err_days), document-server paths first. Returns the set of normalized keys
    known to be PDFs (old and new) and a one-line summary."""
    cache = load_cache()
    today = date.today().isoformat()
    todo = {}
    for u in links:
        p = priority(u)
        if p is None:
            continue
        k = normalize(u)
        hit = cache.get(k)
        if hit and not (hit[0] == "err" and hit[1] and
                        (date.fromisoformat(today) - date.fromisoformat(hit[1])).days >= retry_err_days):
            continue
        if k not in todo or p < todo[k][0]:
            todo[k] = (p, u)
    queue = [u for _, (p, u) in sorted(todo.items(), key=lambda kv: kv[1][0])][:max_new]
    res = probe(queue, budget_s=budget_s) if queue else {}
    for k, v in res.items():
        cache[k] = (v, today)
    save_cache(cache)
    pdfs = {k for k, (v, _) in cache.items() if v == "pdf"}
    new_pdfs = sum(1 for v in res.values() if v == "pdf")
    summary = (f"doc probe: {len(todo):,} unchecked candidates, checked {len(res):,} this run "
               f"({new_pdfs:,} are PDFs); {len(todo) - len(res):,} left for later nights; "
               f"{len(pdfs):,} PDF links known in total.")
    return pdfs, summary
