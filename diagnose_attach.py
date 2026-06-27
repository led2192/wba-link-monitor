#!/usr/bin/env python3
"""
diagnose_attach.py — explain why some report_library rows still have no attached file.

Reads the rows that are classified but still have an empty attachment (i.e. the ones Airtable
could not download), fetches each document_url from here with a browser User-Agent, and reports
the breakdown by outcome:

  ok_pdf        200 and the body really is a PDF  -> recoverable (Airtable's fetcher was blocked,
                                                     but a normal request gets it; download-in-Action
                                                     could attach these)
  not_pdf_200   200 but HTML/other               -> the URL serves a landing/consent page, not the file
  forbidden     401/403                           -> host blocks automated fetching
  not_found     404/410                           -> the document is gone (unrecoverable)
  other_4xx_5xx anything else
  error         timeout / connection refused / SSL

By default it samples 800 of the missing rows (fast, representative). --all checks every one.
Read-only: it writes nothing.

Env: AIRTABLE_TOKEN, AIRTABLE_BASE, AIRTABLE_LIBRARY_TABLE (default report_library),
     AIRTABLE_ATTACH_FIELD (default file)
"""
import argparse, os, sys, time, random, collections
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

API = "https://api.airtable.com/v0"
F_URL = "document_url"; F_TYPE = "source_type"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def airtable_get(url, headers, params, timeout=60, tries=5):
    import requests
    delay = 2
    for attempt in range(1, tries + 1):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=timeout)
        except requests.exceptions.RequestException:
            if attempt == tries:
                raise
            time.sleep(delay); delay = min(delay * 2, 30); continue
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == tries:
                r.raise_for_status()
            time.sleep(delay); delay = min(delay * 2, 30); continue
        r.raise_for_status()
        return r
    return r


def missing_rows(base, token, table, attach_field):
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}"}
    fields = [F_URL, F_TYPE, attach_field]
    params = [("pageSize", "100")] + [("fields[]", f) for f in fields]
    out = []; offset = None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_get(url, headers, p).json()
        for rec in j.get("records", []):
            f = rec.get("fields", {})
            if f.get(F_URL) and f.get(F_TYPE) and not f.get(attach_field):
                out.append(f[F_URL])
        offset = j.get("offset"); time.sleep(0.2)
        if not offset: break
    return out


def check(u):
    import requests
    try:
        r = requests.get(u, headers={"User-Agent": UA}, timeout=20, stream=True,
                         allow_redirects=True)
        sc = r.status_code
        ctype = r.headers.get("Content-Type", "").lower()
        head = b""
        if sc == 200:
            for chunk in r.iter_content(2048):
                head = chunk; break
        r.close()
        is_pdf = head.startswith(b"%PDF") or "application/pdf" in ctype
        if sc == 200 and is_pdf:
            return "ok_pdf"
        if sc == 200:
            return "not_pdf_200"
        if sc in (401, 403):
            return "forbidden"
        if sc in (404, 410):
            return "not_found"
        return "other_4xx_5xx"
    except Exception:
        return "error"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=800)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--workers", type=int, default=20)
    args = ap.parse_args()

    token = os.environ.get("AIRTABLE_TOKEN"); base = os.environ.get("AIRTABLE_BASE")
    table = os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library")
    attach_field = os.environ.get("AIRTABLE_ATTACH_FIELD", "file")
    if not (token and base):
        sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")

    urls = missing_rows(base, token, table, attach_field)
    print(f"{len(urls)} classified rows still missing a file.")
    if not urls:
        return
    if not args.all and len(urls) > args.sample:
        urls = random.sample(urls, args.sample)
        print(f"checking a random sample of {len(urls)} ...\n")
    else:
        print(f"checking all {len(urls)} ...\n")

    counts = collections.Counter()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for fut in as_completed([ex.submit(check, u) for u in urls]):
            counts[fut.result()] += 1

    n = sum(counts.values())
    print("=" * 50)
    order = ["ok_pdf", "not_pdf_200", "forbidden", "not_found", "other_4xx_5xx", "error"]
    for k in order:
        if counts.get(k):
            print(f"  {k:14} {counts[k]:5}  ({counts[k]/n*100:4.1f}%)")
    print("=" * 50)
    rec = counts.get("ok_pdf", 0)
    print(f"recoverable now (ok_pdf -> download-in-Action could attach these): {rec} ({rec/n*100:.1f}%)")
    print(f"likely gone (not_found): {counts.get('not_found',0)} ({counts.get('not_found',0)/n*100:.1f}%)")


if __name__ == "__main__":
    main()
