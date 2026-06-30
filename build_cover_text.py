#!/usr/bin/env python3
"""
build_cover_text.py  (one-off backfill, resumable)

For every report_library row that has a file attachment, extract the text of the
first N pages of the PDF and store it in `cover_text`. The Airtable AI fields can
then read `@cover_text` (a few KB of text) instead of `@file` (the whole PDF),
which is far cheaper in AI credits. The first pages carry the cover, title and
report framing, which is what source_type / source_title need.

Writes `cover_status` for every processed row so the queue drains and the run is
resumable (success or failure, the row leaves the "to do" set):
    ok            -> text extracted, written to cover_text
    no_text       -> PDF parsed but no extractable text (scanned / image-only)
    not_pdf       -> attachment did not start with %PDF
    download_fail -> could not download the attachment
    parse_fail    -> pypdf could not open the file

Rows that end up no_text / not_pdf / parse_fail are the small remainder you can
later push through the full-PDF path or OCR. Everything else classifies off text.

Dry run (no writes): downloads a small sample and prints what it would extract so
you can eyeball quality. Add --commit to run the full backfill.

Env: AIRTABLE_TOKEN, AIRTABLE_BASE,
     AIRTABLE_LIBRARY_TABLE (default report_library),
     AIRTABLE_FILE_FIELD   (default file),
     AIRTABLE_COVER_FIELD  (default cover_text),
     AIRTABLE_COVER_STATUS (default cover_status)
"""
import os, io, sys, time, argparse
from concurrent.futures import ThreadPoolExecutor

import requests
from pypdf import PdfReader

from monitor_core import airtable_request

TOKEN  = os.environ["AIRTABLE_TOKEN"]
BASE   = os.environ["AIRTABLE_BASE"]
LIB    = os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library")
FILE_F = os.environ.get("AIRTABLE_FILE_FIELD", "file")
TEXT_F = os.environ.get("AIRTABLE_COVER_FIELD", "cover_text")
STAT_F = os.environ.get("AIRTABLE_COVER_STATUS", "cover_status")

API = f"https://api.airtable.com/v0/{BASE}/{LIB}"
H   = {"Authorization": f"Bearer {TOKEN}"}
UA  = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/124.0 Safari/537.36"}

MAX_CHARS = 90_000        # stay under Airtable's ~100k long-text cap
DL_TIMEOUT = 45
WORKERS = 20
WRITE_BATCH = 10
WRITE_PAUSE = 0.25


def fetch_todo(page_size):
    """First `page_size` rows that have a file but no cover_status yet."""
    formula = f"AND({{{FILE_F}}}, {{{STAT_F}}} = BLANK())"
    params = [
        ("pageSize", page_size),
        ("filterByFormula", formula),
        ("fields[]", FILE_F),
    ]
    r = airtable_request("GET", API, H, params=params)
    return r.json().get("records", [])


def fetch_sample(n):
    """Any rows that have a file, for a dry-run quality check."""
    formula = f"{{{FILE_F}}}"
    params = [
        ("pageSize", min(n, 100)),
        ("filterByFormula", formula),
        ("fields[]", FILE_F),
    ]
    r = airtable_request("GET", API, H, params=params)
    return r.json().get("records", [])[:n]


def attachment_url(rec):
    cell = rec.get("fields", {}).get(FILE_F) or []
    if not cell:
        return None
    return cell[0].get("url")


def extract_cover(url, pages):
    """Return (status, text). text is '' unless status == 'ok'."""
    try:
        resp = requests.get(url, headers=UA, timeout=DL_TIMEOUT)
        resp.raise_for_status()
        data = resp.content
    except Exception:
        return "download_fail", ""
    if not data[:5].startswith(b"%PDF"):
        return "not_pdf", ""
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                pass
        chunks = []
        for p in reader.pages[:pages]:
            try:
                chunks.append(p.extract_text() or "")
            except Exception:
                chunks.append("")
        text = "\n".join(chunks).strip()
    except Exception:
        return "parse_fail", ""
    if not text:
        return "no_text", ""
    return "ok", text[:MAX_CHARS]


def process_one(rec, pages):
    url = attachment_url(rec)
    if not url:
        return rec["id"], "download_fail", ""
    status, text = extract_cover(url, pages)
    return rec["id"], status, text


def write_batch(updates):
    """updates: list of (record_id, status, text)."""
    for i in range(0, len(updates), WRITE_BATCH):
        chunk = updates[i:i + WRITE_BATCH]
        records = []
        for rid, status, text in chunk:
            fields = {STAT_F: status}
            if status == "ok":
                fields[TEXT_F] = text
            records.append({"id": rid, "fields": fields})
        airtable_request("PATCH", API, H, {"records": records, "typecast": True})
        time.sleep(WRITE_PAUSE)


def run_dry(sample, pages):
    rows = fetch_sample(sample)
    print(f">>> DRY RUN: extracting first {pages} pages from {len(rows)} sample rows (no writes)\n")
    counts = {}
    for rec in rows:
        rid, status, text = process_one(rec, pages)
        counts[status] = counts.get(status, 0) + 1
        preview = text[:600].replace("\n", " ")
        print(f"[{status:13}] {rid}  chars={len(text):>6}  {preview}")
    print("\nsummary:", counts)
    print("\nIf the [ok] previews show the cover / title text, point the AI fields at "
          "@cover_text and run with --commit.")


def run_commit(pages, limit):
    done = 0
    counts = {}
    while True:
        rows = fetch_todo(100)
        if not rows:
            break
        if limit and done + len(rows) > limit:
            rows = rows[:limit - done]
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            results = list(ex.map(lambda r: process_one(r, pages), rows))
        write_batch(results)
        for _, status, _ in results:
            counts[status] = counts.get(status, 0) + 1
        done += len(results)
        print(f"  processed {done} so far  {counts}", flush=True)
        if limit and done >= limit:
            break
    print(f">>> DONE. processed {done} rows. {counts}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=8, help="first N pages to read")
    ap.add_argument("--limit", type=int, default=0, help="cap total rows (0 = all)")
    ap.add_argument("--sample", type=int, default=20, help="dry-run sample size")
    ap.add_argument("--commit", action="store_true", help="write to Airtable")
    args = ap.parse_args()

    if args.commit:
        run_commit(args.pages, args.limit)
    else:
        run_dry(args.sample, args.pages)


if __name__ == "__main__":
    main()
