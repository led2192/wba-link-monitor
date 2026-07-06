#!/usr/bin/env python3
"""
airtable_monitor.py — DAILY monitor. Keeps the Monitored Links alive and detects new reports.

Fetches each page DUE TODAY (by weekday) and in one request resolves both:
  - is the page still alive?   -> status / http_status / last_checked
  - did a NEW document appear? -> diffs the report/PDF links on the page vs what it saw last
    time; new links go to new_links + last_change, and alert_status is set to "new".

The link-extraction, diff, seen_links/detections writing logic lives in monitor_core (shared
with the weekly Playwright monitor). See monitor_core for the seen_links / idempotency fixes.

Due logic is by weekday, NOT "days since last check":
  daily -> every day | mon_wed_fri -> Mon/Wed/Fri | mon_fri -> Mon/Fri | weekly -> Mon |
  monthly/quarterly -> the 1st. MONITOR_FORCE_ALL=true checks everything (use once to baseline).

Env: AIRTABLE_TOKEN, AIRTABLE_BASE, AIRTABLE_TABLE (default monitored_links),
     AIRTABLE_DETECTIONS_TABLE (default detections), MONITOR_FORCE_ALL (optional).
"""
import os, re, sys, time, datetime as dt
from urllib.parse import quote, urlsplit
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
except ImportError:
    sys.exit("pip install requests beautifulsoup4 tldextract")

from monitor_core import (
    API, doc_links, detection_fields, airtable_request, page_language, DOC_ID_Q,
    F_WBA, F_NAME, F_URL, F_FREQ, F_MON, F_STATUS, F_TYPE,
    F_HTTP, F_CHECKED, F_HASH, F_SEEN, F_NEW, F_CHANGE, F_ALERT, F_LANG, F_BROWSER,
)

TOKEN = os.environ.get("AIRTABLE_TOKEN")
BASE  = os.environ.get("AIRTABLE_BASE")
TABLE = os.environ.get("AIRTABLE_TABLE", "monitored_links")
DETECTIONS_TABLE = os.environ.get("AIRTABLE_DETECTIONS_TABLE", "detections")
FORCE_ALL = os.environ.get("MONITOR_FORCE_ALL", "").lower() in ("true", "1", "yes")
# Optional: hand JS-rendered document pages to the weekly browser monitor instead of writing an
# empty baseline from their static shell. OFF by default so turning it on is a deliberate rollout
# (it moves the matched pages into the weekly Playwright lane, which has less capacity). Enable
# with MONITOR_FLAG_SPA=true once you've confirmed the browser job can absorb the extra volume.
FLAG_SPA = os.environ.get("MONITOR_FLAG_SPA", "").lower() in ("true", "1", "yes")
SPA_DOC_TYPES = {"sustainability_page", "reports_hub", "sustainability_report",
                 "policies", "investor_relations"}
# A genuine document library exposes several reports in its HTML. If a JS doc-type page shows
# FEWER than this many real documents in the STATIC shell, its real list is being drawn
# client-side and only boilerplate leaked through (e.g. a site-wide "Modern Slavery Statement"
# /docs?editionId=... link that sits in every page's footer) -> hand it to the browser monitor.
# A plain "has any doc?" test is fooled by that single footer link, which is why abrdn, whose
# footer carries exactly one such link, was not promoted on the first run.
SPA_STATIC_DOC_FLOOR = 3
SPA_MARKERS = re.compile(
    r"__NEXT_DATA__|/_next/|id=[\"']__next[\"']|window\.__NUXT__|data-reactroot|"
    r"ng-version=|window\.__INITIAL_STATE__|__remixContext|_sitecoreJSS", re.I)
if not (TOKEN and BASE):
    sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE environment variables.")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

TODAY = dt.date.today()
WD = TODAY.weekday()  # Mon=0 .. Sun=6


def _visible_text(html):
    """Visible body text of a static HTML document: comments, scripts, styles, noscript and inline
    SVG stripped, tags removed, whitespace collapsed."""
    body = re.sub(r"(?s)<!--.*?-->", " ", html)
    body = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", body)
    text = re.sub(r"(?s)<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", text).strip()


def looks_like_spa(html):
    """True if the static HTML is a JS app shell that builds its content client-side, so a plain
    requests fetch sees no report links. Two signals:
      1. Framework fingerprints (Next/Nuxt/React/Angular/Remix/Sitecore JSS) via SPA_MARKERS.
      2. Emptiness fallback for custom/unrecognized JS stacks (e.g. 2sfg.com): a sizeable page
         whose body carries almost no visible text is a shell — nav chrome plus scripts, with the
         content (document links included) injected client-side. Thresholds are conservative:
         real content pages carry far more than 800 chars of visible text, and small static pages
         under 10 kB of HTML are left alone, so genuinely sparse hubs don't flood the browser lane."""
    if not html:
        return False
    h = html[:200000]
    if SPA_MARKERS.search(h):
        return True
    text = _visible_text(h)
    return len(h) > 10000 and len(text) < 800


DOWNLOAD_BAIT = re.compile(
    r"click here|download|\bpdf\b|view (?:our|the)|read (?:our|the)|see (?:our|the)", re.I)


def has_download_bait(html):
    """Hybrid-page signal: the server-rendered text ADVERTISES documents ('to view our policy
    (click here)', 'download', 'pdf') that the anchor pass did not find as links — static text,
    JS-injected hrefs. Only consulted when n_hard_docs is already below SPA_STATIC_DOC_FLOOR and
    the page is a doc type, so an ordinary page that merely says 'download' somewhere is never
    flagged: the bait only matters on a page whose documents are missing."""
    return bool(html) and bool(DOWNLOAD_BAIT.search(_visible_text(html[:200000])))


def n_hard_docs(current):
    """How many REAL downloadable documents doc_links found (a .pdf or a CMS document-server link),
    as opposed to report-ish navigation links (e.g. /group-sustainability, which matches on the
    word 'sustainab' but is not a document)."""
    c = 0
    for u, _t in current.values():
        base = u.lower().split("?", 1)[0]
        if base.endswith(".pdf") or DOC_ID_Q.search(urlsplit(u).query or ""):
            c += 1
    return c


def due_today(freq):
    f = (freq or "").strip().lower()
    if FORCE_ALL: return True
    if f == "daily": return True
    if f == "mon_wed_fri": return WD in (0, 2, 4)
    if f == "mon_fri": return WD in (0, 4)
    if f == "weekly": return WD == 0
    if f in ("monthly", "quarterly"): return TODAY.day == 1
    return WD == 0  # unknown/empty -> treat as weekly


def fetch(session, url, timeout=20):
    UA = "Mozilla/5.0 (compatible; WBA-LinkMonitor/1.0)"
    try:
        return session.get(url, allow_redirects=True, timeout=timeout, headers={"User-Agent": UA}), None
    except requests.exceptions.SSLError:
        try:
            import urllib3; urllib3.disable_warnings()
            return session.get(url, allow_redirects=True, timeout=timeout,
                               headers={"User-Agent": UA}, verify=False), None
        except Exception:
            return None, "ssl"
    except requests.exceptions.Timeout: return None, "timeout"
    except requests.exceptions.ConnectionError: return None, "connection"
    except Exception: return None, "error"


def get_monitored():
    url = f"{API}/{BASE}/{quote(TABLE)}"
    formula = "AND({%s}=TRUE(), NOT({needs_browser}=TRUE()))" % F_MON
    params = {"pageSize": 100, "filterByFormula": formula}
    out = []; offset = None
    while True:
        if offset: params["offset"] = offset
        r = airtable_request("GET", url, HEADERS, params=params)
        j = r.json(); out.extend(j.get("records", [])); offset = j.get("offset"); time.sleep(0.25)
        if not offset: break
    return out


def patch(updates):
    if not updates:
        return
    url = f"{API}/{BASE}/{quote(TABLE)}"
    headers = {**HEADERS, "Content-Type": "application/json"}
    for i in range(0, len(updates), 10):
        airtable_request("PATCH", url, headers, {"records": updates[i:i + 10], "typecast": True})
        time.sleep(0.2)


def process(rec, sess):
    f = rec.get("fields", {}); u = f.get(F_URL, "")
    r, err = fetch(sess, u)
    upd = {F_CHECKED: TODAY.isoformat()}
    if r is None or r.status_code >= 400:
        upd[F_STATUS] = "dead" if r is not None else "error"
        upd[F_HTTP] = str(r.status_code) if r is not None else ""
        return rec["id"], upd, False, False, []
    upd[F_STATUS] = "redirected" if r.history else "ok"
    upd[F_HTTP] = str(r.status_code)
    lang = page_language(r.text)
    if lang:
        upd[F_LANG] = lang
    current = doc_links(r.text, r.url)
    # A JS-rendered doc page shows no real report in its static shell, only navigation links and
    # maybe a stray footer document. If it's a doc-type page with fewer than SPA_STATIC_DOC_FLOOR
    # real documents AND it either looks like a JS shell (looks_like_spa) or its static text baits
    # documents the anchor pass didn't find (has_download_bait, the hybrid case), hand it to the
    # weekly browser monitor to render rather than writing a misleading baseline. MONITOR_FLAG_SPA.
    if (FLAG_SPA and n_hard_docs(current) < SPA_STATIC_DOC_FLOOR
            and (f.get(F_TYPE) or "").strip().lower() in SPA_DOC_TYPES
            and (looks_like_spa(r.text) or has_download_bait(r.text))):
        upd[F_BROWSER] = True
        return rec["id"], upd, False, False, []
    had_baseline = bool(f.get(F_HASH))   # content_hash present = page visited before
    changed, high, docs = detection_fields(f, upd, current, had_baseline, TODAY)
    return rec["id"], upd, changed, high, docs


def main():
    recs = get_monitored()
    due = [r for r in recs if due_today(r.get("fields", {}).get(F_FREQ))]
    print(f"{len(recs)} monitored links, {len(due)} due today "
          f"({'FORCE_ALL' if FORCE_ALL else TODAY.strftime('%A')}).")
    sess = requests.Session(); updates = []; changed = failed = done = 0
    alerted = 0; promoted = 0
    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = [ex.submit(process, r, sess) for r in due]
        for fut in as_completed(futs):
            try:
                rid, upd, ch, high, _docs = fut.result()   # _docs (per-doc detections) no longer written
            except Exception as e:
                # One pathological page must never kill a 50k-page run: log it, skip it, and let
                # the next pass retry it (its last_checked is untouched, so it stays due).
                failed += 1; done += 1
                print(f">>> WARN: page processing failed ({type(e).__name__}: {e}); row skipped.",
                      flush=True)
                continue
            if upd.get(F_STATUS) in ("dead", "error"): failed += 1
            if upd.get(F_BROWSER): promoted += 1
            if ch: changed += 1
            if high: alerted += 1
            updates.append({"id": rid, "fields": upd}); done += 1
            if len(updates) >= 400:        # checkpoint progress so a long run survives a hiccup
                patch(updates); updates = []
            if done % 200 == 0: print(f"  {done}/{len(due)}")

    print(f"Writing the final {len(updates)} updates back to Airtable ...")
    patch(updates)
    print(f"Done. Pages with new doc links: {changed} (high-signal alerts: {alerted}).  Failed to fetch: {failed}.")
    if promoted:
        print(f"Handed {promoted} JS-rendered doc pages to the weekly browser monitor (needs_browser set).")
    if alerted: print('See them: filter the table by alert_status = "new".')


if __name__ == "__main__":
    main()
