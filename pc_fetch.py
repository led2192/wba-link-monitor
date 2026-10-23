#!/usr/bin/env python3
"""
pc_fetch.py: fetch the files that company sites refuse to GitHub's servers, from an ordinary network.

Runs on the owner's own computer as a GitHub self-hosted runner, attached ONLY to the private
repository wba-text-store (a runner on a public repository would let any pull request run code on
that computer). The workflow there checks out this public code and runs it.

Why: about 3,000 documents have no file (file_status = blocked) because the company's site refuses
requests from cloud addresses. On 23 Oct 2026 a browser-like request and Internet Archive copies from
GitHub's servers recovered 0 of 61 recent sustainability and annual reports (#53, closed): the sites
block by network address. From an ordinary office or home connection most of them open.

For each refused row (report families and 2023+ first, then the ones retried longest ago), up to
--limit, one request at a time per site with a pause, it downloads the PDF with the headers a browser
sends (cookies from the home page, the company page as Referer), and:
  - a PDF of 5 MB or less: uploads it to the row (Airtable's upload limit) and clears file_status,
    so the nightly chain reads, types and dates it like any other file;
  - a bigger PDF: reads its first six pages here and writes cover_text, page_count, doc_language,
    content_hash and file_size_mb, so the AI type, title and year follow; the file itself stays
    unattached (file_status stays blocked) and the note says so;
  - still refused or not a PDF: says so in retry_note.
Every row tried gets retried_on and retry_note. Never gets around a login, a captcha or a paywall.
Dry run by default (prints, writes pc_fetch.md); --commit writes.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE; QA: AIRTABLE_QA_TOKEN (optional). Args: --limit 300 --minutes 90
"""
import argparse, collections, datetime as dt, hashlib, io, os, re, threading, time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, unquote

import requests

import qa
from monitor_core import airtable_request, lang_name

API = "https://api.airtable.com/v0"
LIB = "report_library"
UPLOAD_MAX = 5 * 1024 * 1024
DOWNLOAD_MAX = 150 * 1024 * 1024
MIN_PDF = 20_000
COVER_PAGES = 6
COVER_CHARS = 10_000
SITE_PAUSE = 2.0
FAMILY = {"Sustainability Report", "ESG Report", "CSR Report", "Integrated Report", "Impact Report",
          "Annual Report", "10K Form", "Registration Document"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9", "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "same-origin", "Sec-Fetch-User": "?1"}
_locks = collections.defaultdict(threading.Lock)


# ---- pure helpers ---------------------------------------------------------------------------------
def filename_for(url, lib_id=""):
    name = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]) or (lib_id or "document")
    name = re.sub(r"[^\w.\-]+", "_", name)[:120]
    return name if name.lower().endswith(".pdf") else name + ".pdf"


def plan(code, body, complete):
    """What to do with one download: 'upload', 'read_here', or ('refused'|'not_pdf'|'too_small'|'too_big')."""
    if code is None or code >= 400:
        return "refused"
    if body[:4] != b"%PDF":
        return "not_pdf"
    if not complete:
        return "too_big"
    if len(body) < MIN_PDF:
        return "too_small"
    return "upload" if len(body) <= UPLOAD_MAX else "read_here"


def note_for(today, outcome, detail=""):
    return f"{today}: {outcome}" + (f" ({detail})" if detail else "")


def order_key(row):
    recent = row.get("effective_type") in FAMILY and (row.get("year") or 0) >= 2023
    return (not recent, row.get("retried_on") or "", -(row.get("year") or 0))


# ---- network --------------------------------------------------------------------------------------
def fetch(url, referer=None):
    """(status, bytes, complete) with a browser-like request, one request at a time per site."""
    host = urlsplit(url).netloc
    lock = _locks[host]
    with lock:
        try:
            sess = requests.Session()
            p = urlsplit(url)
            home = f"{p.scheme}://{p.netloc}/"
            try:
                sess.get(home, headers=HEADERS, timeout=30)
            except requests.RequestException:
                pass
            r = sess.get(url, headers=dict(HEADERS, Referer=referer or home), timeout=90, stream=True)
            body, complete = b"", True
            for chunk in r.iter_content(65536):
                body += chunk
                if len(body) > DOWNLOAD_MAX:
                    complete = False
                    break
            r.close()
            return r.status_code, body, complete
        except requests.RequestException:
            return None, b"", False
        finally:
            time.sleep(SITE_PAUSE)


def read_cover(data):
    """(text, page_count, language) of the first pages of a PDF given as bytes."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    chunks = []
    for page in reader.pages[:COVER_PAGES]:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            chunks.append("")
    text = re.sub(r"\s+", " ", "\n".join(chunks)).strip()[:COVER_CHARS]
    lang = None
    try:
        from langdetect import detect
        if sum(ch.isalpha() for ch in text) >= 30:
            lang = lang_name(detect(text))
    except Exception:
        pass
    return text, len(reader.pages), lang


# ---- Airtable -------------------------------------------------------------------------------------
def fetch_rows(token, base, limit):
    url = f"{API}/{base}/{LIB}"
    headers = {"Authorization": f"Bearer {token}"}
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
    out.sort(key=order_key)
    return out[:limit] if limit else out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--minutes", type=float, default=90, help="stop starting new files after this long")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    token, base = os.environ.get("AIRTABLE_TOKEN"), os.environ.get("AIRTABLE_BASE")
    if not (token and base):
        raise SystemExit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    today = dt.date.today().isoformat()
    run = qa.Run("pc_fetch.py")
    rows = fetch_rows(token, base, a.limit)
    print(f">>> {len(rows):,} refused files to try from this computer", flush=True)
    deadline = time.time() + a.minutes * 60
    counts, results = collections.Counter(), []

    def one(row):
        if time.time() > deadline:
            return row, "not_tried", None
        code, body, complete = fetch(row["document_url"], row.get("found_on"))
        what = plan(code, body, complete)
        return row, what, (code, body if what in ("upload", "read_here") else b"")

    from recover_attach import upload
    from concurrent.futures import as_completed
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(one, r) for r in rows]
        for i, fut in enumerate(as_completed(futs), 1):
            row, what, extra = fut.result()
            counts[what] += 1
            fields = None
            if what == "upload":
                fields = {"retried_on": today, "retry_note": note_for(today, "attached: downloaded from the office-network runner")}
                if a.commit:
                    ok = upload(base, token, "file", row["_id"], filename_for(row["document_url"], row.get("library_id")), extra[1])
                    if ok:
                        fields["file_status"] = None
                    else:
                        fields["retry_note"] = note_for(today, "downloaded from the office-network runner, upload failed")
                        counts["upload_failed"] += 1
            elif what == "read_here":
                data = extra[1]
                try:
                    text, pages, lang = read_cover(data)
                except Exception as e:
                    text, pages, lang = "", None, None
                    counts["unreadable"] += 1
                fields = {"retried_on": today, "retry_note": note_for(
                    today, "read on the office-network runner; too big to attach", f"{len(data) / 1048576:.1f} MB"),
                    "content_hash": hashlib.sha256(data).hexdigest(), "file_size_mb": round(len(data) / 1048576, 2)}
                if text:
                    fields.update({"cover_text": text, "cover_status": "ok"})
                    if lang:
                        fields["doc_language"] = lang
                if pages:
                    fields["page_count"] = pages
            elif what != "not_tried":
                detail = {"refused": f"status {extra[0] or 'no answer'}", "not_pdf": "the address does not serve a PDF",
                          "too_small": "file too small to be the document", "too_big": "over 150 MB"}[what]
                fields = {"retried_on": today, "retry_note": note_for(today, "still refused from the office network"
                                                                      if what == "refused" else "not attached", detail)}
            results.append((row, what))
            if fields and a.commit:
                airtable_request("PATCH", f"{API}/{base}/{LIB}", {"Authorization": f"Bearer {token}"},
                                 {"records": [{"id": row["_id"], "fields": fields}], "typecast": True})
                time.sleep(0.2)
            if i % 25 == 0:
                print(f"  {i}/{len(rows)} {dict(counts)}", flush=True)

    tried = [r for r, w in results if w != "not_tried"]
    got = counts["upload"] + counts["read_here"]
    recent = [r for r, w in results if w != "not_tried" and r.get("effective_type") in FAMILY and (r.get("year") or 0) >= 2023]
    got_recent = sum(1 for r, w in results if w in ("upload", "read_here") and r in recent)
    md = ["### Refused files fetched from the office network" + ("" if a.commit else " (dry run)"), "",
          f"Tried: {len(tried):,}. Reached: {got:,} ({got / (len(tried) or 1):.0%}); of the {len(recent):,} 2023+ "
          f"sustainability and annual reports among them, {got_recent:,}.", "",
          "| Outcome | Files |", "| --- | --- |",
          f"| attached (5 MB or less) | {counts['upload']:,} |",
          f"| read here, too big to attach | {counts['read_here']:,} |",
          f"| still refused | {counts['refused']:,} |",
          f"| not a PDF / too small / over 150 MB | {counts['not_pdf'] + counts['too_small'] + counts['too_big']:,} |",
          f"| not tried (time budget) | {counts['not_tried']:,} |", ""]
    open("pc_fetch.md", "w", encoding="utf-8").write("\n".join(md))
    print("\n".join(md), flush=True)
    run.finish("ok", rows_in=len(rows), rows_out=got, counters=dict(counts))
    run.write_step_summary("ok", dict(counts), title="Office-network fetch")


if __name__ == "__main__":
    main()
