#!/usr/bin/env python3
"""
blocked_retry.py: once a month, try again to get the files whose company site refuses our robot.

Takes report_library rows with no file and file_status = blocked (report families and 2023+ first, then
the ones retried longest ago), up to --limit, and tries for each, in order:
  1. headers   a full browser-like request: the headers a browser sends, cookies from a first visit to
               the home page, the company page the document was found on as Referer. A PDF of 5 MB or
               less is uploaded to the row (Airtable's upload limit).
  2. archive   the Internet Archive's copy of the exact address (Wayback Machine, original bytes).
  3. variants  an archived copy of the same file at another address of the company: the same path
               with any query string or scheme (CDX prefix search), and, when the file name is
               distinctive (a year or digits, 12+ characters), the same file name anywhere on the
               company's domain (CDX domain search).
An archived copy is attached by its address (Airtable fetches it; no 5 MB limit), its address is kept
in archive_url, and file_status is cleared, so the cover text, AI type, year and full text follow like
for any other file. Every row tried gets retried_on and a plain-words retry_note.

The blocked probe (blocked_probe.py, Oct 2026) measured these free routes at about 17% of refused
documents; the archive variants were not measured then. A dry run on a pull request measures them.

One request at a time per site and to the archive, with pauses. Never gets around a login, a captcha
or a paywall: a refused browser request stays refused.
Dry run by default (prints, writes blocked_retry.md); --commit writes to Airtable.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE; QA: AIRTABLE_QA_TOKEN (optional). Args: --limit 1500 --workers 8
"""
import argparse, collections, datetime as dt, os, re, threading, time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlsplit

import requests

import qa
from blocked_probe import BROWSER_HEADERS, PLAIN, FAMILY
from monitor_core import airtable_request
from recover_attach import upload

API = "https://api.airtable.com/v0"
TOKEN, BASE = os.environ.get("AIRTABLE_TOKEN"), os.environ.get("AIRTABLE_BASE")
LIB = "report_library"
CDX = "https://web.archive.org/cdx/search/cdx"
UPLOAD_MAX = 5 * 1024 * 1024
MIN_PDF = 20_000                      # smaller "PDFs" are error pages or stubs
ARCHIVE_PAUSE = 1.0
SITE_PAUSE = 2.0
_locks = collections.defaultdict(threading.Lock)


# ---- pure helpers ---------------------------------------------------------------------------------
def basename(url):
    return urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]


def distinctive(name):
    """A file name specific enough to identify one document across a domain."""
    stem = re.sub(r"\.pdf$", "", name or "", flags=re.I)
    return len(stem) >= 12 and bool(re.search(r"\d", stem)) and not re.fullmatch(r"[0-9a-f\-]{16,}", stem, re.I)


def registered_domain(host):
    parts = (host or "").lower().split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "ac", "ne", "or"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def raw_copy(ts, original):
    return f"https://web.archive.org/web/{ts}id_/{original}"


def parse_cdx(js):
    """CDX JSON rows (first row = header) -> [(timestamp, original, length)], newest first."""
    if not js or len(js) < 2:
        return []
    head = js[0]
    i_ts, i_or = head.index("timestamp"), head.index("original")
    i_len = head.index("length") if "length" in head else None
    rows = [(r[i_ts], r[i_or], int(r[i_len]) if i_len is not None and str(r[i_len]).isdigit() else 0)
            for r in js[1:]]
    return sorted(rows, key=lambda r: r[0], reverse=True)


def note_for(today, outcome, detail):
    return f"{today}: {outcome}" + (f" ({detail})" if detail else "")


# ---- network --------------------------------------------------------------------------------------
def polite(key, pause):
    lock = _locks[key]
    lock.acquire()
    return lock, pause


def done(lock_pause):
    lock, pause = lock_pause
    time.sleep(pause)
    lock.release()


def get_body(url, headers, sess=None, referer=None, cap=UPLOAD_MAX + 1):
    """(status, bytes up to cap, complete?) or (None, b'', False)."""
    s = sess or requests
    h = dict(headers, **({"Referer": referer} if referer else {}))
    lp = polite(urlsplit(url).netloc, SITE_PAUSE if "archive.org" not in url else ARCHIVE_PAUSE)
    try:
        r = s.get(url, headers=h, timeout=45, allow_redirects=True, stream=True)
        body, complete = b"", True
        for chunk in r.iter_content(65536):
            body += chunk
            if len(body) > cap:
                complete = False
                break
        r.close()
        return r.status_code, body, complete
    except requests.RequestException:
        return None, b"", False
    finally:
        done(lp)


def cdx(params):
    lp = polite("archive.org", ARCHIVE_PAUSE)
    try:
        r = requests.get(CDX, params=dict(params, output="json"), headers=PLAIN, timeout=25)
        return parse_cdx(r.json()) if r.ok and r.text.strip() else []
    except (requests.RequestException, ValueError):
        return []
    finally:
        done(lp)


def archive_pdf(original):
    """Check an archived copy really is a PDF: (ok, size)."""
    code, head, _ = get_body(original, PLAIN, cap=4096)
    return bool(code == 200 and head[:5] == b"%PDF"), len(head)


# ---- the routes -----------------------------------------------------------------------------------
def try_headers(row):
    url = row["document_url"]
    sess = requests.Session()
    parts = urlsplit(url)
    home = f"{parts.scheme}://{parts.netloc}/"
    get_body(home, BROWSER_HEADERS, sess, cap=200_000)
    code, body, complete = get_body(url, BROWSER_HEADERS, sess, referer=row.get("found_on") or home)
    if code == 200 and body[:5] == b"%PDF":
        if complete and len(body) >= MIN_PDF:
            return {"route": "headers", "data": body}
        if not complete:
            return {"route": "headers_too_big", "detail": "a browser request works but the file is over 5 MB"}
    return {"route": None, "detail": f"browser request {code or 'failed'}"}


def try_archive(row):
    url = row["document_url"]
    recent_report = row.get("effective_type") in FAMILY and (row.get("year") or 0) >= 2023
    p = urlsplit(url)
    path_key = f"{p.netloc}{p.path}"
    # exact address and its query/scheme variants
    rows = cdx({"url": path_key, "matchType": "prefix", "filter": ["statuscode:200", "mimetype:application/pdf"],
                "fl": "timestamp,original,length", "limit": "-5"})
    rows = [r for r in rows if basename(r[1]) == basename(url)]
    how = "Internet Archive copy"
    if not rows and recent_report and distinctive(basename(url)):     # domain search is slow: only where it matters
        name = re.escape(basename(url))
        rows = cdx({"url": registered_domain(p.netloc), "matchType": "domain",
                    "filter": ["statuscode:200", "mimetype:application/pdf", f"original:.*/{name}(\\?.*)?$"],
                    "fl": "timestamp,original,length", "limit": "-3"})
        how = "Internet Archive copy at another address"
    for ts, original, length in rows:
        if length and length < MIN_PDF // 4:          # CDX length is compressed; skip obvious stubs
            continue
        copy = raw_copy(ts, original)
        ok, _ = archive_pdf(copy)
        if ok:
            return {"route": "archive", "url": copy, "detail": f"{how} of {ts[:4]}-{ts[4:6]}-{ts[6:8]}"}
    return {"route": None, "detail": "no archive copy"}


def decide(row, today, headers_fn=try_headers, archive_fn=try_archive):
    """What to do with one refused row: (action, fields, counter). Network through the two route functions."""
    h = headers_fn(row)
    if h["route"] == "headers":
        return "upload", {"data": h["data"], "note": note_for(today, "attached: a browser request works", "")}, "headers"
    a = archive_fn(row)
    if a["route"] == "archive":
        return "attach_url", {"archive_url": a["url"], "note": note_for(today, "attached from an " + a["detail"], "")}, "archive"
    tried = "; ".join(d for d in (h.get("detail"), a.get("detail")) if d)
    counter = "too_big_headers" if h["route"] == "headers_too_big" else "still_refused"
    return "note", {"note": note_for(today, "still refused", tried)}, counter


# ---- Airtable -------------------------------------------------------------------------------------
def fetch_rows(limit):
    url = f"{API}/{BASE}/{LIB}"
    headers = {"Authorization": f"Bearer {TOKEN}"}
    fields = ["library_id", "document_url", "found_on", "effective_type", "year", "retried_on"]
    params = [("pageSize", "100"), ("filterByFormula",
              "AND(NOT({discard}), {file}='', {file_status}='blocked', {document_url}!='')")] + [("fields[]", f) for f in fields]
    out, offset = [], None
    while True:
        p = params + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", url, headers, params=p).json()
        for r in j.get("records", []):
            f = r.get("fields", {}); f["_id"] = r["id"]; out.append(f)
        offset = j.get("offset")
        if not offset:
            break
        time.sleep(0.12)
    out.sort(key=lambda d: (d.get("effective_type") not in FAMILY or (d.get("year") or 0) < 2023,
                            d.get("retried_on") or "", -(d.get("year") or 0)))
    return out[:limit] if limit else out


def patch(rows):
    url = f"{API}/{BASE}/{LIB}"
    for i in range(0, len(rows), 10):
        airtable_request("PATCH", url, {"Authorization": f"Bearer {TOKEN}"}, {"records": rows[i:i + 10]})
        time.sleep(0.25)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1500)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--minutes", type=float, default=300, help="stop starting new files after this long")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    if not (TOKEN and BASE):
        raise SystemExit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    today = dt.date.today().isoformat()
    run = qa.Run("blocked_retry.py")
    rows = fetch_rows(a.limit)
    print(f">>> {len(rows):,} refused files to retry", flush=True)
    results, counts = [], collections.Counter()

    deadline = time.time() + a.minutes * 60

    def one(row):
        if time.time() > deadline:
            return row, ("skip", {"note": ""}, "not_tried")
        try:
            return row, decide(row, today)
        except Exception as e:                       # one bad row never stops the run
            return row, ("note", {"note": note_for(today, "retry failed", type(e).__name__)}, "error")

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, (row, res) in enumerate(ex.map(one, rows), 1):
            results.append((row, res))
            counts[res[2]] += 1
            if i % 100 == 0:
                print(f"  {i}/{len(rows)}  {dict(counts)}", flush=True)

    tried = [(r, res) for r, res in results if res[0] != "skip"]
    recent = [r for r, res in tried if r.get("effective_type") in FAMILY and (r.get("year") or 0) >= 2023]
    got_recent = sum(1 for r, res in results if res[0] in ("upload", "attach_url")
                     and r.get("effective_type") in FAMILY and (r.get("year") or 0) >= 2023)
    n = len(tried) or 1
    md = ["### Blocked files retry" + ("" if a.commit else " (dry run)"), "",
          f"Refused files tried: {len(tried):,}. Recovered: {counts['headers'] + counts['archive']:,} "
          f"({(counts['headers'] + counts['archive']) / n:.0%}); of the {len(recent):,} 2023+ sustainability "
          f"and annual reports among them, {got_recent:,}.", "",
          "| Outcome | Files |", "| --- | --- |",
          f"| attached by a browser-like request | {counts['headers']:,} |",
          f"| attached from an Internet Archive copy | {counts['archive']:,} |",
          f"| reachable but over 5 MB (not attached) | {counts['too_big_headers']:,} |",
          f"| still refused | {counts['still_refused']:,} |",
          f"| retry error | {counts['error']:,} |",
          f"| not tried (time budget) | {counts['not_tried']:,} |", ""]
    open("blocked_retry.md", "w").write("\n".join(md))
    print("\n".join(md), flush=True)

    if a.commit:
        updates = []
        for row, (action, f, _) in results:
            if action == "skip":
                continue
            fields = {"retried_on": today, "retry_note": f["note"][:250]}
            if action == "upload":
                ok = upload(BASE, TOKEN, "file", row["_id"], basename(row["document_url"]) or "document.pdf", f["data"])
                if ok:
                    fields["file_status"] = None
                else:
                    fields["retry_note"] = note_for(today, "a browser request works but the upload failed", "")
            elif action == "attach_url":
                fields.update({"file": [{"url": f["archive_url"]}], "archive_url": f["archive_url"], "file_status": None})
            updates.append({"id": row["_id"], "fields": fields})
        patch(updates)
    run.finish("ok", rows_in=len(rows), rows_out=counts["headers"] + counts["archive"], counters=dict(counts))
    run.write_step_summary("ok", dict(counts), title="Blocked files retry")


if __name__ == "__main__":
    main()
