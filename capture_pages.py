#!/usr/bin/env python3
"""
capture_pages.py — snapshot monitored web pages as printed PDFs ("Webpage" sources).

Why: web pages themselves are disclosure sources (the taxonomy's "Webpage" type with its own
sub-types), but they only enter the pipeline if something turns them into a document. This
script renders each flagged page in headless Chromium (JavaScript included, so client-rendered
content like 2sfg.com is captured in full), prints it to an A4 PDF, and upserts it as a row of a
report_library-shaped table (default: webpage_library). cover_text and doc_language are written
at capture time from the rendered text, so no build_cover_text pass is needed for snapshots and
the AI agents can run on them immediately.

Scope is a checkbox: monitored_links.capture_page. Analysts tick/untick freely; --seed does the
initial bulk-tick (monitored sustainability_page + policies pages whose page_language is English
or blank).

Refresh & versioning: each capture stores page_hash (SHA-1 of the rendered visible text) and
captured_on. A page becomes due again when captured_on is older than --refresh-days (default 90).
On re-render:
  hash unchanged -> only captured_on is touched: NO new PDF, so storage grows only when the
                    content actually changed, not with the calendar.
  hash changed   -> a new PDF is APPENDED to the attachment field. Airtable attachment fields
                    keep the previous files, so the field itself is the version history (the
                    filename carries the capture date).
Upload uses Airtable's uploadAttachment endpoint; its payload cap means PDFs above ~3.5 MB raw
are re-printed without backgrounds, and if still too big the row is written anyway (hash,
cover_text, doc_language) with file_status="too_big" so classification still works without the
file. Dead/error pages get a stub row so they are retried only after the refresh window, not on
every run.

Modes:
  --seed              bulk-tick capture_page (add --commit to write; alone = count only)
  --limit N           pages per run, never-captured first, then stalest first (0 = all due)
  --refresh-days D    re-render pages whose capture is older than D days (default 90)
  --commit            actually render and write; without it, prints the due counts and exits

library_id = <wba_id>-<sha1(normalize(page_url))[:10]> — the report_library scheme, so each page
has one stable row across runs.
"""

import os, sys, time, base64, hashlib, argparse, threading, datetime as dt
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from monitor_core import airtable_request, normalize, LANG_NAMES

API         = "https://api.airtable.com/v0"
CONTENT_API = "https://content.airtable.com/v0"

TOKEN     = os.environ.get("AIRTABLE_TOKEN")
BASE      = os.environ.get("AIRTABLE_BASE")
SRC_TABLE = os.environ.get("AIRTABLE_TABLE", "monitored_links")
CAP_TABLE = os.environ.get("AIRTABLE_WEBPAGE_TABLE", "webpage_library")
FILE_F    = os.environ.get("AIRTABLE_ATTACH_FIELD", "file")
STATUS_F  = os.environ.get("AIRTABLE_STATUS_FIELD", "file_status")
if not (TOKEN and BASE):
    sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE environment variables.")
H = {"Authorization": f"Bearer {TOKEN}"}

# monitored_links fields
F_URL = "url"; F_WBA = "wba_id"; F_NAME = "company_name"; F_TYPE = "type"; F_LID = "link_id"
F_FLAG = "capture_page"; F_PLANG = "page_language"
SEED_TYPES = ("sustainability_page", "policies")

WORKERS        = 3
NAV_TIMEOUT_MS = 45000
SETTLE_MS      = 3000
MAX_RAW_PDF    = 3_500_000   # raw bytes; base64 must stay under uploadAttachment's payload cap
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TODAY = dt.date.today()

_tl = threading.local()


def get_ctx():
    """One Playwright + Chromium per worker thread, reused across its pages (the
    playwright_monitor pattern)."""
    if not hasattr(_tl, "ctx"):
        from playwright.sync_api import sync_playwright
        _tl.pw = sync_playwright().start()
        _tl.browser = _tl.pw.chromium.launch(headless=True)
        _tl.ctx = _tl.browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900})
    return _tl.ctx


def lib_id(wba, url):
    return f"{(str(wba).strip() or 'NA')}-{hashlib.sha1(normalize(url).encode()).hexdigest()[:10]}"


def text_hash(text):
    """Hash of the rendered visible text, whitespace- and case-insensitive, so cosmetic
    re-serialization doesn't count as a content change."""
    return hashlib.sha1(" ".join((text or "").lower().split()).encode()).hexdigest()


def lang_of(text):
    if not text or len(text.strip()) < 40:
        return None
    try:
        from langdetect import detect, DetectorFactory
        DetectorFactory.seed = 0
        return LANG_NAMES.get(detect(text[:4000]))
    except Exception:
        return None


def fetch_all(table, formula, fields):
    url = f"{API}/{BASE}/{quote(table)}"
    params = {"pageSize": 100, "filterByFormula": formula, "fields[]": list(fields)}
    out = []; offset = None
    while True:
        if offset:
            params["offset"] = offset
        j = airtable_request("GET", url, H, params=params).json()
        out.extend(j.get("records", []))
        offset = j.get("offset"); time.sleep(0.21)
        if not offset:
            break
    return out


def seed(commit):
    """Tick capture_page on monitored sustainability_page/policies rows whose page_language is
    English or blank and that are not flagged yet. One-off; analysts curate afterwards."""
    type_or = ", ".join(f"{{{F_TYPE}}}='{t}'" for t in SEED_TYPES)
    formula = (f"AND({{monitor}}=TRUE(), NOT({{{F_FLAG}}}=TRUE()), OR({type_or}), "
               f"OR({{{F_PLANG}}}='English', {{{F_PLANG}}}=BLANK()))")
    rows = fetch_all(SRC_TABLE, formula, [F_URL])
    print(f"--seed: {len(rows)} monitored {'+'.join(SEED_TYPES)} pages (English/blank) not yet flagged.")
    if not commit:
        print("Dry-run. Re-run with --commit to tick capture_page on them.")
        return
    url = f"{API}/{BASE}/{quote(SRC_TABLE)}"
    for i in range(0, len(rows), 10):
        chunk = [{"id": r["id"], "fields": {F_FLAG: True}} for r in rows[i:i + 10]]
        airtable_request("PATCH", url, H, {"records": chunk, "typecast": True})
        time.sleep(0.21)
        if (i // 10) % 50 == 0:
            print(f"  ticked {min(i + 10, len(rows))}/{len(rows)}")
    print(f"Done: capture_page set on {len(rows)} rows.")


def render(url):
    """Render in Chromium and print. Returns (visible_text, pdf_bytes_or_None, status)."""
    page = get_ctx().new_page()
    try:
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception:
            resp = page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(SETTLE_MS)
        code = resp.status if resp else None
        if code and code >= 400:
            return "", None, f"dead {code}"
        try:
            text = page.inner_text("body", timeout=8000)
        except Exception:
            text = ""
        page.emulate_media(media="screen")      # print the page as seen, not its print stylesheet
        pdf = page.pdf(format="A4", print_background=True)
        if len(pdf) > MAX_RAW_PDF:
            slim = page.pdf(format="A4", print_background=False)
            if len(slim) < len(pdf):
                pdf = slim
        return text, pdf, "ok"
    finally:
        try:
            page.close()
        except Exception:
            pass


def upload_pdf(rec_id, pdf, filename):
    if len(pdf) > MAX_RAW_PDF:
        return False
    payload = {"contentType": "application/pdf",
               "file": base64.b64encode(pdf).decode(),
               "filename": filename}
    airtable_request("POST", f"{CONTENT_API}/{BASE}/{rec_id}/{quote(FILE_F)}/uploadAttachment",
                     H, payload, timeout=120)
    return True


def work(rec, ex):
    """Capture one page. `ex` is the existing webpage_library row summary or None.
    Returns a tally key: captured / captured_no_file / unchanged / dead / error / skipped."""
    f = rec.get("fields", {})
    u = (f.get(F_URL) or "").strip()
    if not u:
        return "skipped"
    lid = lib_id(f.get(F_WBA, ""), u)
    cap_url = f"{API}/{BASE}/{quote(CAP_TABLE)}"
    try:
        text, pdf, st = render(u)
    except Exception as e:
        text, pdf, st = "", None, f"error {type(e).__name__}"

    if st != "ok":
        # Stub the attempt so the page is retried after the refresh window, not on every run.
        if ex:
            airtable_request("PATCH", cap_url, H, {"records": [{"id": ex["id"], "fields": {
                "captured_on": TODAY.isoformat(), STATUS_F: st}}], "typecast": True})
        else:
            airtable_request("POST", cap_url, H, {"records": [{"fields": {
                "library_id": lid, "wba_id": f.get(F_WBA, ""), "company_name": f.get(F_NAME, ""),
                "document_url": u, "found_on": u, "page_type": f.get(F_TYPE, ""),
                "source_link_id": f.get(F_LID, ""), "source_page": [rec["id"]],
                "source_type": "Webpage", "captured_on": TODAY.isoformat(),
                "page_hash": "", STATUS_F: st}}], "typecast": True})
        return st.split()[0]

    h = text_hash(text)
    if ex and ex.get("page_hash") == h:
        airtable_request("PATCH", cap_url, H, {"records": [{"id": ex["id"], "fields": {
            "captured_on": TODAY.isoformat()}}]})
        return "unchanged"

    fields = {"library_id": lid, "wba_id": f.get(F_WBA, ""), "company_name": f.get(F_NAME, ""),
              "document_url": u, "found_on": u, "page_type": f.get(F_TYPE, ""),
              "source_link_id": f.get(F_LID, ""), "source_page": [rec["id"]],
              "source_type": "Webpage", "match_confidence": "high",
              "doc_year": str(TODAY.year), "captured_on": TODAY.isoformat(),
              "page_hash": h, "cover_text": (text or "")[:10000],
              "cover_status": "ok" if text.strip() else "no_text"}
    lang = lang_of(text)
    if lang:
        fields["doc_language"] = lang

    if ex:
        rec_id = ex["id"]
        airtable_request("PATCH", cap_url, H, {"records": [{"id": rec_id, "fields": fields}],
                                               "typecast": True})
    else:
        j = airtable_request("POST", cap_url, H, {"records": [{"fields": fields}],
                                                  "typecast": True}).json()
        rec_id = j["records"][0]["id"]

    attached = False
    if pdf:
        try:
            attached = upload_pdf(rec_id, pdf, f"{lid}_{TODAY:%Y%m%d}.pdf")
        except Exception:
            attached = False
    airtable_request("PATCH", cap_url, H, {"records": [{"id": rec_id, "fields": {
        STATUS_F: "attached" if attached else "too_big"}}], "typecast": True})
    return "captured" if attached else "captured_no_file"


def main():
    ap = argparse.ArgumentParser(description="Print monitored web pages to PDF snapshots.")
    ap.add_argument("--seed", action="store_true", help="bulk-tick capture_page (with --commit)")
    ap.add_argument("--limit", type=int, default=1200, help="pages this run (0 = all due)")
    ap.add_argument("--refresh-days", type=int, default=90)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    if args.seed:
        seed(args.commit)
        return

    src = fetch_all(SRC_TABLE, f"AND({{{F_FLAG}}}=TRUE(), {{monitor}}=TRUE())",
                    [F_URL, F_WBA, F_NAME, F_TYPE, F_LID])
    ex_rows = fetch_all(CAP_TABLE, "TRUE()", ["library_id", "page_hash", "captured_on"])
    existing = {}
    for r in ex_rows:
        fl = r.get("fields", {})
        if fl.get("library_id"):
            existing[fl["library_id"]] = {"id": r["id"], "page_hash": fl.get("page_hash", ""),
                                          "captured_on": fl.get("captured_on", "")}

    cutoff = (TODAY - dt.timedelta(days=args.refresh_days)).isoformat()
    due_new, due_stale = [], []
    for r in src:
        lid = lib_id(r["fields"].get(F_WBA, ""), (r["fields"].get(F_URL) or ""))
        ex = existing.get(lid)
        if not ex:
            due_new.append(r)
        elif (ex.get("captured_on") or "") <= cutoff:
            due_stale.append(r)
    due_stale.sort(key=lambda r: existing[lib_id(r["fields"].get(F_WBA, ""),
                                                 r["fields"].get(F_URL, ""))]["captured_on"] or "")
    due = due_new + due_stale
    if args.limit:
        due = due[:args.limit]
    print(f"flagged+monitored: {len(src)} | never captured: {len(due_new)} | "
          f"stale (> {args.refresh_days}d): {len(due_stale)} | this run: {len(due)}")
    if not args.commit:
        print("Dry-run (no --commit): nothing rendered.")
        return

    tally = {}; done = 0; lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(work, r, existing.get(lib_id(r["fields"].get(F_WBA, ""),
                                                         r["fields"].get(F_URL, "")))): r
                for r in due}
        for fut in as_completed(futs):
            try:
                res = fut.result()
            except Exception as e:
                res = f"error {type(e).__name__}"
            key = res.split()[0]
            with lock:
                tally[key] = tally.get(key, 0) + 1; done += 1
                if done % 25 == 0:
                    print(f"  {done}/{len(due)}  {tally}", flush=True)
    print(f">>> DONE. {done} pages. {tally}")


if __name__ == "__main__":
    main()
