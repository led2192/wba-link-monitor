#!/usr/bin/env python3
"""
playwright_monitor.py — WEEKLY browser-based monitor for the hard pages.

Targets rows plain requests can't read: status dead/error (403/429 bot-blocks, timeouts) plus
every row marked needs_browser. Renders each in headless Chromium, which passes most bot
challenges and runs the JavaScript that builds report lists (the year-dropdown case: the default
newest-year view gets read).

Same write contract as the daily monitor (shared via monitor_core):
  status / http_status / final_url / last_checked / content_hash
  seen_links / new_links / last_change / alert_status="new" on detection
Plus its own: needs_browser=True on every page it reads (the daily job then skips it).

Env: AIRTABLE_TOKEN, AIRTABLE_BASE, AIRTABLE_TABLE (default monitored_links),
     AIRTABLE_DETECTIONS_TABLE (default detections).
Setup (see playwright.yml): pip install -r requirements.txt playwright; playwright install --with-deps chromium
"""
import os, re, sys, time, random, threading, datetime as dt
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
except ImportError:
    sys.exit("pip install requests beautifulsoup4 tldextract")
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("pip install playwright  &&  playwright install --with-deps chromium")

from monitor_core import (
    API, normalize, doc_links, detection_fields, airtable_request,
    F_WBA, F_NAME, F_URL, F_TYPE, F_STATUS, F_HTTP, F_FINAL,
    F_CHECKED, F_HASH, F_SEEN, F_NEW, F_CHANGE, F_ALERT, F_BROWSER,
)

TOKEN = os.environ.get("AIRTABLE_TOKEN")
BASE  = os.environ.get("AIRTABLE_BASE")
TABLE = os.environ.get("AIRTABLE_TABLE", "monitored_links")
DETECTIONS_TABLE = os.environ.get("AIRTABLE_DETECTIONS_TABLE", "detections")
if not (TOKEN and BASE):
    sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE environment variables.")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

TODAY = dt.date.today()
WORKERS = 3
NAV_TIMEOUT_MS = 15000
SETTLE_MS = 1500
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def get_targets():
    url = f"{API}/{BASE}/{quote(TABLE)}"
    formula = ("AND({monitor}=TRUE(), OR({status}='dead', {status}='error', {%s}=TRUE()))" % F_BROWSER)
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


_tl = threading.local()
def get_page():
    """One Playwright+Chromium per worker thread, reused across its pages."""
    if not hasattr(_tl, "ctx"):
        _tl.pw = sync_playwright().start()
        _tl.browser = _tl.pw.chromium.launch(headless=True)
        _tl.ctx = _tl.browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900})
        _tl.ctx.route(re.compile(r"\.(png|jpe?g|gif|webp|svg|ico|woff2?|ttf|mp4|webm)(\?|$)", re.I),
                      lambda route: route.abort())
    return _tl.ctx


def process(rec):
    f = rec.get("fields", {}); u = f.get(F_URL, "")
    upd = {F_CHECKED: TODAY.isoformat()}
    page = None
    try:
        page = get_page().new_page()
        resp = page.goto(u, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(SETTLE_MS)
        code = resp.status if resp else None
        if code and code >= 400:
            upd[F_STATUS] = "dead"; upd[F_HTTP] = str(code)
            return rec["id"], upd, False, False, [], False
        final = page.url
        upd[F_HTTP] = str(code or "")
        upd[F_FINAL] = final
        upd[F_STATUS] = "redirected" if normalize(final) != normalize(u) else "ok"
        current = doc_links(page.content(), final)
        had_baseline = bool(f.get(F_HASH))          # content_hash present = visited before
        changed, high, docs = detection_fields(f, upd, current, had_baseline, TODAY)
        upd[F_BROWSER] = True                        # browser owns this page from now on
        return rec["id"], upd, changed, high, docs, True
    except Exception:
        upd[F_STATUS] = "error"
        return rec["id"], upd, False, False, [], False
    finally:
        if page:
            try: page.close()
            except Exception: pass


def main():
    recs = get_targets()
    random.shuffle(recs)   # avoid hammering one domain in a burst (the 429s)
    print(f"{len(recs)} hard pages to render with a real browser ({WORKERS} workers).")
    updates = []; rescued = changed = still_dead = 0; done = 0
    alerted = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(process, r) for r in recs]
        for fut in as_completed(futs):
            rid, upd, ch, high, _docs, ok = fut.result()   # _docs (per-doc detections) no longer written
            if ok: rescued += 1
            elif upd.get(F_STATUS) in ("dead", "error"): still_dead += 1
            if ch: changed += 1
            if high: alerted += 1
            updates.append({"id": rid, "fields": upd}); done += 1
            if len(updates) >= 200:        # checkpoint progress so a long run survives a hiccup
                patch(updates); updates = []
            if done % 100 == 0: print(f"  {done}/{len(recs)}")
    print(f"Writing the final {len(updates)} updates back to Airtable ...")
    patch(updates)
    print(f"Done. Readable in a real browser: {rescued}.  Still dead/error: {still_dead}.  "
          f"Pages with new doc links: {changed} (high-signal alerts: {alerted}).")
    print("Rescued pages are now marked needs_browser and owned by this weekly job.")


if __name__ == "__main__":
    main()
