#!/usr/bin/env python3
"""
build_cover_text.py  (one-off backfill, resumable)

For every report_library row that has a file attachment, open the PDF and write:
    cover_text   -> text of the first N pages (cheap input for the AI fields)
    page_count   -> total number of pages in the PDF
    doc_language -> ISO language code detected from the cover text (en, de, ja, ...)
    cover_status -> outcome (the real reason)

cover_status values:
    ok            -> text extracted; cover_text + page_count + doc_language written
    no_text       -> PDF parsed but no extractable text (scanned / image-only); page_count written
    parse_fail    -> file arrived COMPLETE (ends in %%EOF) but pypdf still cannot read it -> genuinely broken
    truncated     -> download arrived INCOMPLETE after retries (no %%EOF / short body) -> transient, retry later
    not_pdf       -> attachment did not start with %PDF
    download_fail -> could not download at all after retries -> transient, retry later

page_count is ALWAYS written (real count, or 0 when there is no usable PDF), so the queue
(driven by page_count being empty) terminates. page_count = 0 means "see cover_status".

Why the completeness check: under heavy concurrency the attachment CDN can throttle and return
a body cut short with a 200. That body starts with %PDF but lacks the trailing %%EOF, so pypdf
fails. We verify Content-Length and the %%EOF tail, retry, and only then mark 'truncated' (NOT a
permanent failure). This also avoids handing pypdf broken files, whose recovery scan is very slow.

Modes:
    (no flag)   dry run: download a small sample and print what would be extracted (no writes)
    --commit    run the backfill (queue = rows with a file and empty page_count)
    --reclaim   clear page_count + cover_status on rows previously marked as a (possibly transient)
                failure, so a later --commit reprocesses them with the hardened downloader

Env: AIRTABLE_TOKEN, AIRTABLE_BASE,
     AIRTABLE_LIBRARY_TABLE   (default report_library),
     AIRTABLE_FILE_FIELD      (default file),
     AIRTABLE_COVER_FIELD     (default cover_text),
     AIRTABLE_COVER_STATUS    (default cover_status),
     AIRTABLE_PAGECOUNT_FIELD (default page_count),
     AIRTABLE_DOCLANG_FIELD   (default doc_language)

requirements.txt must include: pypdf, langdetect
"""
import os, io, re, time, argparse
from concurrent.futures import ThreadPoolExecutor

import requests
from pypdf import PdfReader

from monitor_core import airtable_request, lang_name

try:
    from langdetect import detect as _ld_detect, DetectorFactory
    DetectorFactory.seed = 0
    _HAVE_LD = True
except Exception:
    _HAVE_LD = False

TOKEN  = os.environ["AIRTABLE_TOKEN"]
BASE   = os.environ["AIRTABLE_BASE"]
LIB    = os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library")
FILE_F = os.environ.get("AIRTABLE_FILE_FIELD", "file")
TEXT_F = os.environ.get("AIRTABLE_COVER_FIELD", "cover_text")
STAT_F = os.environ.get("AIRTABLE_COVER_STATUS", "cover_status")
PAGE_F = os.environ.get("AIRTABLE_PAGECOUNT_FIELD", "page_count")
LANG_F = os.environ.get("AIRTABLE_DOCLANG_FIELD", "doc_language")

API = f"https://api.airtable.com/v0/{BASE}/{LIB}"
H   = {"Authorization": f"Bearer {TOKEN}"}
UA  = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/124.0 Safari/537.36"}

MAX_CHARS = 90_000        # Airtable long-text safety cap
COVER_CHARS = 10_000      # cost cap: enough for cover/title/framing; clips dense docs
DL_TIMEOUT = 60
DL_TRIES = 3
WORKERS = 6              # gentle: high concurrency was triggering CDN throttling / truncation
WRITE_BATCH = 10
WRITE_PAUSE = 0.25
# failure states that --reclaim puts back into the queue
RETRYABLE = ("parse_fail", "not_pdf", "truncated", "download_fail")


def detect_lang(text):
    if not _HAVE_LD or not text:
        return None
    if sum(ch.isalpha() for ch in text) < 30:    # number/code-only covers fool the detector (often -> Greek)
        return None
    try:
        return lang_name(_ld_detect(text))        # detect over the whole cover, then map code -> name
    except Exception:
        return None


def fetch_todo(page_size):
    formula = f"AND({{{FILE_F}}}, {{{PAGE_F}}} = BLANK())"
    params = [("pageSize", page_size), ("filterByFormula", formula), ("fields[]", FILE_F)]
    r = airtable_request("GET", API, H, params=params)
    return r.json().get("records", [])


def fetch_sample(n):
    params = [("pageSize", min(n, 100)), ("filterByFormula", f"{{{FILE_F}}}"), ("fields[]", FILE_F)]
    r = airtable_request("GET", API, H, params=params)
    return r.json().get("records", [])[:n]


def attachment_url(rec):
    cell = rec.get("fields", {}).get(FILE_F) or []
    return cell[0].get("url") if cell else None


def download_pdf(url):
    """Return (kind, data). kind in {'ok','not_pdf','truncated','download_fail'}.
    Verifies the body is a COMPLETE PDF (Content-Length match and %%EOF tail); retries
    incomplete or failed downloads before giving up."""
    last = "download_fail"
    delay = 1.0
    for _ in range(DL_TRIES):
        try:
            resp = requests.get(url, headers=UA, timeout=DL_TIMEOUT)
            resp.raise_for_status()
            data = resp.content
        except Exception:
            last = "download_fail"; time.sleep(delay); delay *= 2; continue
        if not data[:5].startswith(b"%PDF"):
            return "not_pdf", b""           # genuinely not a PDF (or an error page)
        clen = resp.headers.get("Content-Length")
        short = bool(clen and clen.isdigit() and len(data) < int(clen))
        no_eof = b"%%EOF" not in data[-2048:]
        if short or no_eof:                  # incomplete body -> throttled/cut; retry
            last = "truncated"; time.sleep(delay); delay *= 2; continue
        return "ok", data
    return last, b""


def extract_cover(url, pages):
    """Return (status, text, page_count). page_count is None unless the PDF parsed."""
    kind, data = download_pdf(url)
    if kind != "ok":
        return kind, "", None                # not_pdf / truncated / download_fail
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                pass
        chunks = []
        for p in reader.pages[:pages]:        # v1 path: only resolves the first N pages
            try:
                chunks.append(p.extract_text() or "")
            except Exception:
                chunks.append("")
        text = re.sub(r"\s+", " ", "\n".join(chunks)).strip()   # collapse whitespace (token economy)
    except Exception:
        return "parse_fail", "", None        # complete file, but genuinely unreadable
    # page count is best-effort; it must NEVER gate the text extraction above
    try:
        n_pages = len(reader.pages)
    except Exception:
        n_pages = None
    if not text:
        return "no_text", "", n_pages
    return "ok", text[:COVER_CHARS], n_pages


def process_one(rec, pages):
    url = attachment_url(rec)
    if not url:
        return rec["id"], "download_fail", "", None, None
    status, text, n_pages = extract_cover(url, pages)
    lang = detect_lang(text) if status == "ok" else None
    return rec["id"], status, text, n_pages, lang


def write_batch(updates):
    for i in range(0, len(updates), WRITE_BATCH):
        chunk = updates[i:i + WRITE_BATCH]
        records = []
        for rid, status, text, n_pages, lang in chunk:
            fields = {STAT_F: status, PAGE_F: n_pages if n_pages is not None else 0}
            if status == "ok":
                fields[TEXT_F] = text
                if lang:
                    fields[LANG_F] = lang
            records.append({"id": rid, "fields": fields})
        airtable_request("PATCH", API, H, {"records": records, "typecast": True})
        time.sleep(WRITE_PAUSE)


def run_dry(sample, pages):
    rows = fetch_sample(sample)
    print(f">>> DRY RUN: first {pages} pages from {len(rows)} sample rows (no writes)")
    print(f"    langdetect available: {_HAVE_LD}\n")
    counts = {}
    for rec in rows:
        rid, status, text, n_pages, lang = process_one(rec, pages)
        counts[status] = counts.get(status, 0) + 1
        preview = text[:600].replace("\n", " ")
        pg = n_pages if n_pages is not None else "-"
        print(f"[{status:13}] {rid}  pages={pg!s:>4}  lang={lang or '-':<6}  chars={len(text):>6}  {preview}")
    print("\nsummary:", counts)
    print("\nIf the [ok] previews look right, point the AI fields at @cover_text and run with --commit.")


def run_commit(pages, limit):
    done, counts = 0, {}
    while True:
        rows = fetch_todo(100)
        if not rows:
            break
        if limit and done + len(rows) > limit:
            rows = rows[:limit - done]
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            results = list(ex.map(lambda r: process_one(r, pages), rows))
        write_batch(results)
        for _, status, _, _, _ in results:
            counts[status] = counts.get(status, 0) + 1
        done += len(results)
        print(f"  processed {done} so far  {counts}", flush=True)
        if limit and done >= limit:
            break
    print(f">>> DONE. processed {done} rows. {counts}")


def run_reclaim():
    """Put previously-failed rows back into the queue (clear page_count + cover_status), so a
    later --commit reprocesses them. We key off page_count = 0, which is the universal failure
    sentinel (every failure writes it, no real PDF has 0 pages, no_text keeps its real count),
    so this catches all failures regardless of what cover_status currently holds."""
    formula = f"AND({{{PAGE_F}}} = 0, NOT({{{PAGE_F}}} = BLANK()))"
    cleared = 0
    while True:
        params = [("pageSize", 100), ("filterByFormula", formula), ("fields[]", PAGE_F)]
        r = airtable_request("GET", API, H, params=params)
        rows = r.json().get("records", [])
        if not rows:
            break
        for i in range(0, len(rows), WRITE_BATCH):
            recs = [{"id": x["id"], "fields": {PAGE_F: None, STAT_F: None}} for x in rows[i:i + WRITE_BATCH]]
            airtable_request("PATCH", API, H, {"records": recs, "typecast": True})
            time.sleep(WRITE_PAUSE)
        cleared += len(rows)
        print(f"  cleared {cleared}", flush=True)
    print(f">>> RECLAIM done. {cleared} rows put back into the queue. Now run with --commit.")


def run_diagnose(n, pages):
    """Inspect rows straight from the queue (page_count empty) and print, per file, what actually
    comes down the wire: real header, Content-Type, completeness, encryption, and the exact pypdf
    error. No writes. This tells us WHY files fail instead of guessing."""
    rows = fetch_todo(min(n, 100))
    print(f">>> DIAGNOSE: inspecting {len(rows)} rows from the queue (no writes)\n")
    summ = {}
    for rec in rows:
        url = attachment_url(rec)
        fn = ((rec.get("fields", {}).get(FILE_F) or [{}])[0].get("filename", "") or "")[:45]
        if not url:
            print(f"{rec['id']}  NO URL"); summ["no_url"] = summ.get("no_url", 0) + 1; continue
        try:
            resp = requests.get(url, headers=UA, timeout=DL_TIMEOUT)
            data = resp.content
        except Exception as e:
            print(f"{rec['id']}  DOWNLOAD ERROR {type(e).__name__}: {e}")
            summ["dl_err"] = summ.get("dl_err", 0) + 1; continue
        ctype = resp.headers.get("Content-Type", "")
        clen = resp.headers.get("Content-Length", "")
        eof = b"%%EOF" in data[-2048:]
        is_pdf = data[:5].startswith(b"%PDF")
        verdict = "ok"
        line = (f"{rec['id']}  fn={fn!r}\n"
                f"    http={resp.status_code} ctype={ctype!r} clen={clen} got={len(data)} "
                f"pdf_header={is_pdf} eof_tail={eof}\n"
                f"    head={data[:20]!r}\n    tail={data[-32:]!r}")
        if is_pdf:
            try:
                rd = PdfReader(io.BytesIO(data))
                enc = rd.is_encrypted
                if enc:
                    try: rd.decrypt("")
                    except Exception: pass
                note = ""
                txt_chars = None
                try:                                   # probe A: first-page text (the v1 path)
                    txt_chars = len((rd.pages[0].extract_text() or "").strip())
                except Exception as e:
                    note += f" TEXT_ERR={type(e).__name__}:{str(e)[:45]}"
                len_pages = None
                try:                                   # probe B: full page count (the v2 line)
                    len_pages = len(rd.pages)
                except Exception as e:
                    note += f" LEN_ERR={type(e).__name__}:{str(e)[:45]}"
                line += f"\n    pypdf: encrypted={enc} first_page_text_chars={txt_chars} len_pages={len_pages}{note}"
                if txt_chars:
                    verdict = "text_ok_len_fail" if len_pages is None else "ok"
                elif len_pages is not None:
                    verdict = "no_text"
                else:
                    verdict = "parse_fail"
            except Exception as e:
                line += f"\n    pypdf: READER_ERROR {type(e).__name__}: {str(e)[:90]}"
                verdict = "reader_fail"
        else:
            verdict = "not_pdf"
        summ[verdict] = summ.get(verdict, 0) + 1
        print(line + f"\n    => {verdict}\n")
    print("verdict summary:", summ)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=6, help="first N pages to read")
    ap.add_argument("--limit", type=int, default=0, help="cap total rows (0 = all)")
    ap.add_argument("--sample", type=int, default=20, help="dry-run sample size")
    ap.add_argument("--commit", action="store_true", help="write to Airtable")
    ap.add_argument("--reclaim", action="store_true", help="requeue previously-failed rows, then exit")
    ap.add_argument("--diagnose", type=int, default=0, metavar="N", help="inspect N queue rows verbosely, then exit")
    args = ap.parse_args()
    if args.diagnose:
        run_diagnose(args.diagnose, args.pages)
    elif args.reclaim:
        run_reclaim()
    elif args.commit:
        run_commit(args.pages, args.limit)
    else:
        run_dry(args.sample, args.pages)


if __name__ == "__main__":
    main()
