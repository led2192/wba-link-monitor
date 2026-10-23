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
Every row tried gets retried_on and retry_note.

Refused pages too (--pages, default 150 a run): monitored report pages (reports hub, sustainability,
investor relations, policies) whose site refuses cloud servers (status blocked, or dead with 401, 403
or 429), the ones read longest ago first (pc_checked). Each is read here and its document links go to
seen_links and new_links exactly as the nightly monitor would write them; the nightly harvest then
turns them into documents, and the files the cloud cannot download come back to this runner as
refused files. The page's status is left to the nightly monitor.

Meant to run several times a day (every 4 hours); runs that find the computer off simply wait.
Several computers can run it: each run takes its own slice (oldest first, rows already tried today
are skipped). Never gets around a login, a captcha or a paywall.
Dry run by default (prints, writes pc_fetch.md); --commit writes.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE; QA: AIRTABLE_QA_TOKEN (optional).
Args: --limit 300 --pages 150 --minutes 90
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
    today = dt.date.today().isoformat()
    out = [r for r in out if (r.get("retried_on") or "") < today]   # another run today already tried it
    out.sort(key=order_key)
    return out[:limit] if limit else out


PAGE_TYPES = ("reports_hub", "sustainability_page", "sustainability_report", "investor_relations", "policies")


def fetch_pages(token, base, limit):
    import monitor_core as MC
    url = f"{API}/{base}/monitored_links"
    headers = {"Authorization": f"Bearer {token}"}
    fields = [MC.F_URL, MC.F_TYPE, MC.F_WBA, MC.F_NAME, MC.F_SEEN, MC.F_NEW, MC.F_HASH, "pc_checked"]
    formula = ("AND({monitor}, OR({status}='blocked', AND({status}='dead', OR({http_status}=401, {http_status}=403, "
               "{http_status}=429))))")
    params = [("pageSize", "100"), ("filterByFormula", formula)] + [("fields[]", f) for f in fields]
    out, offset = [], None
    while True:
        p = params + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", url, headers, params=p).json()
        out += [r for r in j.get("records", []) if (r.get("fields", {}).get(MC.F_TYPE) or "") in PAGE_TYPES]
        offset = j.get("offset")
        if not offset:
            break
        time.sleep(0.12)
    today = dt.date.today().isoformat()
    out = [r for r in out if (r["fields"].get("pc_checked") or "") < today]
    out.sort(key=lambda r: r["fields"].get("pc_checked") or "")
    return out[:limit]


def read_page(rec):
    """(fields to write, outcome) for one refused page read from this network."""
    import monitor_core as MC
    f = rec["fields"]
    today = dt.date.today()
    upd = {"pc_checked": today.isoformat()}
    host = urlsplit(f.get(MC.F_URL, "")).netloc
    with _locks[host]:
        try:
            r = requests.get(f[MC.F_URL], headers=HEADERS, timeout=45)
        except requests.RequestException:
            r = None
        finally:
            time.sleep(SITE_PAUSE)
    if r is None or r.status_code >= 400:
        return upd, "refused"
    return links_to_fields(rec, r.text, r.url)


_robots = {}


def robots_allows(url):
    """True unless the site's robots.txt disallows this path for every robot. Unreadable = allowed
    (the plain request above already reached robots.txt or not)."""
    from urllib import robotparser
    p = urlsplit(url)
    root = f"{p.scheme}://{p.netloc}"
    if root not in _robots:
        rp = robotparser.RobotFileParser()
        try:
            r = requests.get(root + "/robots.txt", headers=HEADERS, timeout=20)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except requests.RequestException:
            rp.parse([])
        _robots[root] = rp
    return _robots[root].can_fetch("*", url)


class Browser:
    """Microsoft Edge (already on every Windows), driven without a window, for what a plain request could
    not reach (owner's decision, 23 Oct 2026). To a site it is an ordinary browser on an ordinary
    connection: automatic "checking your browser" pages pass by themselves. Safeguards: a path the
    site's robots.txt disallows is skipped; nothing here clicks, types or solves, so a captcha or a
    login stays refused; one page at a time with a pause. PC_BROWSER=chrome uses Google Chrome."""

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._p = sync_playwright().start()
        self._b = self._p.chromium.launch(channel=os.environ.get("PC_BROWSER", "msedge"), headless=True)
        self.ctx = self._b.new_context(accept_downloads=True, locale="en-US", user_agent=UA)
        return self

    def __exit__(self, *exc):
        for x in (getattr(self, "ctx", None), getattr(self, "_b", None)):
            try:
                x.close()
            except Exception:
                pass
        try:
            self._p.stop()
        except Exception:
            pass

    def _visit(self, page, url):
        resp = page.goto(url, timeout=45000, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)          # an automatic browser check finishes and reloads by itself
        return resp

    def fetch_pdf(self, url, referer=None):
        """(status, bytes, complete) like fetch(): the company page first (as a visitor arrives), then the file."""
        page = self.ctx.new_page()
        try:
            try:
                self._visit(page, referer or f"{urlsplit(url).scheme}://{urlsplit(url).netloc}/")
            except Exception:
                pass
            try:
                r = self.ctx.request.get(url, timeout=90000, headers={"Referer": referer or ""})
                body = r.body()
                if r.status < 400 and body[:4] == b"%PDF":
                    return r.status, body[:DOWNLOAD_MAX + 1], len(body) <= DOWNLOAD_MAX
                status = r.status
            except Exception:
                status = None
            try:                              # some sites only hand the file to a page that navigates to it
                with page.expect_download(timeout=60000) as dl:
                    try:
                        page.goto(url, timeout=60000)
                    except Exception:
                        pass
                body = open(dl.value.path(), "rb").read()
                return 200, body[:DOWNLOAD_MAX + 1], len(body) <= DOWNLOAD_MAX
            except Exception:
                return status, b"", False
        finally:
            page.close()
            time.sleep(SITE_PAUSE)

    def page_html(self, url):
        """(status, html, final url) of a page as the browser renders it."""
        page = self.ctx.new_page()
        try:
            resp = self._visit(page, url)
            return (resp.status if resp else None), page.content(), page.url
        except Exception:
            return None, "", url
        finally:
            page.close()
            time.sleep(SITE_PAUSE)


def links_to_fields(rec, html, final_url):
    """(fields, outcome) from a page's HTML, as the nightly monitor would write them."""
    import monitor_core as MC
    f = rec["fields"]
    today = dt.date.today()
    upd = {"pc_checked": today.isoformat()}
    current = MC.doc_links(html, final_url)
    if not current:
        return upd, "no_links"
    changed, _high, _docs = MC.detection_fields(f, upd, current, bool(f.get(MC.F_HASH)), today)
    return upd, ("new_links" if changed else "read")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--minutes", type=float, default=90, help="stop starting new files after this long")
    ap.add_argument("--pages", type=int, default=150, help="refused report pages to read (0 = none)")
    ap.add_argument("--browser", type=int, default=100,
                    help="files a plain request could not reach to try again in Edge without a window (0 = none); "
                         "refused pages of the run are tried in Edge too")
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

    from recover_attach import upload

    def one(row):
        if time.time() > deadline:
            return row, "not_tried", None
        code, body, complete = fetch(row["document_url"], row.get("found_on"))
        what = plan(code, body, complete)
        return row, what, (code, body if what in ("upload", "read_here") else b"")

    def handle(row, what, extra, browser=False):
        counts[what] += 1
        fields = None
        if what == "upload":
            fields = {"retried_on": today, "retry_note": note_for(today, "attached: downloaded from the office-network runner" + (" (browser)" if browser else ""))}
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
                today, "read on the office-network runner" + (" (browser)" if browser else "") + "; too big to attach", f"{len(data) / 1048576:.1f} MB"),
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

    deferred = []
    from concurrent.futures import as_completed
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(one, r) for r in rows]
        for i, fut in enumerate(as_completed(futs), 1):
            row, what, extra = fut.result()
            if what == "refused" and a.browser and len(deferred) < a.browser:
                deferred.append((row, extra))           # second chance in the browser, below
                continue
            handle(row, what, extra)
            if i % 25 == 0:
                print(f"  {i}/{len(rows)} {dict(counts)}", flush=True)

    # second pass: what a plain request could not reach, in Edge without a window (one at a time)
    browser_ok = None
    if deferred:
        print(f">>> {len(deferred):,} refused files to try in the browser", flush=True)
        try:
            with Browser() as b:
                browser_ok = True
                for row, extra in deferred:
                    if time.time() > deadline:
                        handle(row, "refused", extra)
                        continue
                    if not robots_allows(row["document_url"]):
                        counts["robots_disallow"] += 1
                        handle(row, "refused", (extra[0], b""))
                        continue
                    code, body, complete = b.fetch_pdf(row["document_url"], row.get("found_on"))
                    what = plan(code, body, complete)
                    if what in ("upload", "read_here"):
                        counts["by_browser"] += 1
                    handle(row, what, (code, body if what in ("upload", "read_here") else b""), browser=True)
        except Exception as e:
            browser_ok = f"{type(e).__name__}: {str(e)[:150]}"
            print(f">>> browser not available: {browser_ok}", flush=True)
            done_ids = {r["_id"] for r, _ in results}
            for row, extra in deferred:
                if row["_id"] not in done_ids:
                    handle(row, "refused", extra)

    # refused pages: read here, links handed to the nightly harvest
    pcounts = collections.Counter()
    if a.pages and time.time() < deadline:
        refused_pages = []
        pages = fetch_pages(token, base, a.pages)
        print(f">>> {len(pages):,} refused report pages to read from this computer", flush=True)
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            for rec, (upd, what) in zip(pages, ex.map(lambda r: read_page(r) if time.time() < deadline
                                                       else ({}, "not_tried"), pages)):
                if what == "refused" and a.browser and browser_ok in (None, True):
                    refused_pages.append(rec)
                    continue
                pcounts[what] += 1
                if upd and a.commit:
                    airtable_request("PATCH", f"{API}/{base}/monitored_links", {"Authorization": f"Bearer {token}"},
                                     {"records": [{"id": rec["id"], "fields": upd}], "typecast": True})
                    time.sleep(0.2)

        if refused_pages:
            print(f">>> {len(refused_pages):,} refused pages to try in the browser", flush=True)
            today_s = dt.date.today().isoformat()
            try:
                with Browser() as b:
                    for rec in refused_pages:
                        url_ = rec["fields"].get("url", "")
                        if time.time() > deadline or not robots_allows(url_):
                            upd, what = {"pc_checked": today_s}, "refused"
                        else:
                            code, html, final = b.page_html(url_)
                            if code is None or code >= 400 or not html:
                                upd, what = {"pc_checked": today_s}, "refused"
                            else:
                                upd, what = links_to_fields(rec, html, final)
                                pcounts["by_browser"] += 1
                        pcounts[what] += 1
                        if a.commit:
                            airtable_request("PATCH", f"{API}/{base}/monitored_links", {"Authorization": f"Bearer {token}"},
                                             {"records": [{"id": rec["id"], "fields": upd}], "typecast": True})
                            time.sleep(0.2)
            except Exception as e:
                print(f">>> browser not available for pages: {type(e).__name__}", flush=True)
                for rec in refused_pages:
                    pcounts["refused"] += 1
                    if a.commit:
                        airtable_request("PATCH", f"{API}/{base}/monitored_links", {"Authorization": f"Bearer {token}"},
                                         {"records": [{"id": rec["id"], "fields": {"pc_checked": today_s}}], "typecast": True})

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
          f"| of which reached only by the browser (Edge, no window) | {counts['by_browser']:,} |",
          f"| still refused | {counts['refused']:,} |",
          f"| of which skipped because robots.txt disallows them | {counts['robots_disallow']:,} |",
          f"| not a PDF / too small / over 150 MB | {counts['not_pdf'] + counts['too_small'] + counts['too_big']:,} |",
          f"| not tried (time budget) | {counts['not_tried']:,} |", "",
          f"Refused report pages read: {sum(v for k, v in pcounts.items() if k != 'not_tried'):,}: "
          f"{pcounts['new_links']:,} with new document links, {pcounts['read']:,} read with nothing new, "
          f"{pcounts['no_links']:,} with no document links, {pcounts['refused']:,} refused here too; "
          f"{pcounts['by_browser']:,} of the pages read only by the browser.", "",
          "Browser: " + ("not needed this run" if browser_ok is None and not counts['by_browser'] else
                         "used" if browser_ok is True else f"not available ({browser_ok})"), ""]
    open("pc_fetch.md", "w", encoding="utf-8").write("\n".join(md))
    print("\n".join(md), flush=True)
    run.finish("ok", rows_in=len(rows), rows_out=got, counters={**dict(counts), "pages": dict(pcounts)})
    run.write_step_summary("ok", dict(counts), title="Office-network fetch")


if __name__ == "__main__":
    main()
