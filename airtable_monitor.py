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
import os, sys, time, datetime as dt
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
except ImportError:
    sys.exit("pip install requests beautifulsoup4 tldextract")

from monitor_core import (
    API, doc_links, detection_fields, post_detections, airtable_request,
    F_WBA, F_NAME, F_URL, F_FREQ, F_MON, F_STATUS, F_TYPE,
    F_HTTP, F_CHECKED, F_HASH, F_SEEN, F_NEW, F_CHANGE, F_ALERT,
)

TOKEN = os.environ.get("AIRTABLE_TOKEN")
BASE  = os.environ.get("AIRTABLE_BASE")
TABLE = os.environ.get("AIRTABLE_TABLE", "monitored_links")
DETECTIONS_TABLE = os.environ.get("AIRTABLE_DETECTIONS_TABLE", "detections")
FORCE_ALL = os.environ.get("MONITOR_FORCE_ALL", "").lower() in ("true", "1", "yes")
if not (TOKEN and BASE):
    sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE environment variables.")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

TODAY = dt.date.today()
WD = TODAY.weekday()  # Mon=0 .. Sun=6


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
    current = doc_links(r.text, r.url)
    had_baseline = bool(f.get(F_HASH))   # content_hash present = page visited before
    changed, high, docs = detection_fields(f, upd, current, had_baseline, TODAY)
    return rec["id"], upd, changed, high, docs


def main():
    recs = get_monitored()
    due = [r for r in recs if due_today(r.get("fields", {}).get(F_FREQ))]
    print(f"{len(recs)} monitored links, {len(due)} due today "
          f"({'FORCE_ALL' if FORCE_ALL else TODAY.strftime('%A')}).")
    sess = requests.Session(); updates = []; changed = failed = done = 0
    alerted = 0; detections = []
    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = [ex.submit(process, r, sess) for r in due]
        for fut in as_completed(futs):
            rid, upd, ch, high, docs = fut.result()
            if upd.get(F_STATUS) in ("dead", "error"): failed += 1
            if ch: changed += 1
            if high: alerted += 1
            detections.extend(docs)
            updates.append({"id": rid, "fields": upd}); done += 1
            if len(updates) >= 400:        # checkpoint progress so a long run survives a hiccup
                patch(updates); updates = []
            if len(detections) >= 200:
                post_detections(detections, BASE, TOKEN, DETECTIONS_TABLE); detections = []
            if done % 200 == 0: print(f"  {done}/{len(due)}")

    print(f"Writing the final {len(updates)} updates back to Airtable ...")
    patch(updates)
    post_detections(detections, BASE, TOKEN, DETECTIONS_TABLE)
    print(f"Done. Pages with new doc links: {changed} (high-signal alerts: {alerted}).  Failed to fetch: {failed}.")
    if alerted: print('See them: filter the table by alert_status = "new".')


if __name__ == "__main__":
    main()
