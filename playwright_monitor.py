#!/usr/bin/env python3
"""
playwright_monitor.py — browser-based monitor for the hard pages, as a ROTATING QUEUE.

Targets rows plain requests can't read: status dead/error (403/429 bot-blocks, timeouts) plus
every row marked needs_browser. Renders each in headless Chromium, which passes most bot
challenges and runs the JavaScript that builds report lists.

Why a queue and not "run until done": GitHub kills any job at 6 hours (this workflow's own cap is
350 min), and the needs_browser set outgrew that window in July 2026 — runs died at the cap and,
because targets were randomly shuffled, which pages got covered each week was a lottery. Now
targets are ordered OLDEST-FIRST by last_checked (never-visited first), each run self-limits to
--max-pages and finishes cleanly, and the next run continues where coverage is stalest. Two
scheduled runs a week sweep the whole queue; no page can starve.

Targeted mode (--wba / --contains) is the debug lever: it takes ALL monitored rows of the target
(ignoring needs_browser/status, so "render everything ACS has" is one dispatch and minutes, not
hours), and it does NOT newly mark pages as needs_browser — debug sweeps must not grow the weekly
queue. In normal queue mode the browser still claims every page it reads (needs_browser=True) so
the daily requests monitor skips it.

Same write contract as the daily monitor (shared via monitor_core):
  status / http_status / final_url / last_checked / content_hash
  seen_links / new_links / last_change / alert_status="new" on detection

Env: AIRTABLE_TOKEN, AIRTABLE_BASE, AIRTABLE_TABLE (default monitored_links).
Args: --wba PT_00019[,PT_00020]   --contains grupoacs   --max-pages 6000 (0 = no cap)
      --workers 4
Setup (see playwright.yml): pip install -r requirements.txt playwright; playwright install --with-deps chromium
"""
import os, re, sys, time, random, argparse, threading, collections, datetime as dt
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
    API, normalize, doc_links, detection_fields, airtable_request, reg_domain,
    F_WBA, F_NAME, F_URL, F_TYPE, F_STATUS, F_HTTP, F_FINAL,
    F_CHECKED, F_HASH, F_SEEN, F_NEW, F_CHANGE, F_ALERT, F_BROWSER,
)

TOKEN = os.environ.get("AIRTABLE_TOKEN")
BASE  = os.environ.get("AIRTABLE_BASE")
TABLE = os.environ.get("AIRTABLE_TABLE", "monitored_links")
if not (TOKEN and BASE):
    sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE environment variables.")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

TODAY = dt.date.today()
NAV_TIMEOUT_MS = 15000
SETTLE_MS = 1500
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

OWN_PAGES = True    # queue mode claims pages for the browser; targeted mode (set False) does not
RESCUE_SHARE = float(os.environ.get("RESCUE_SHARE", "0.25") or 0.25)   # max fraction of a batch spent on dead/error rescues
RECYCLE_EVERY = int(os.environ.get("RECYCLE_EVERY", "150") or 150)     # relaunch each worker's Chromium every N pages


def build_formula(wba_csv, contains):
    """Airtable filterByFormula for the run's targets. Targeted mode = ALL monitored rows of the
    target; queue mode = the browser's own set (needs_browser or dead/error)."""
    clauses = ["{monitor}=TRUE()"]
    wbas = [w.strip() for w in (wba_csv or "").split(",") if w.strip()]
    if wbas:
        ors = ",".join("{wba_id}='%s'" % w.replace("'", "") for w in wbas)
        clauses.append(ors if len(wbas) == 1 else f"OR({ors})")
    if contains:
        needle = contains.strip().lower().replace("'", "")
        clauses.append(f"FIND('{needle}', LOWER({{url}}))")
    if not (wbas or contains):
        clauses.append("OR({status}='dead', {status}='error', {%s}=TRUE())" % F_BROWSER)
    return clauses[0] if len(clauses) == 1 else "AND(" + ", ".join(clauses) + ")"


def get_targets(formula):
    url = f"{API}/{BASE}/{quote(TABLE)}"
    params = {"pageSize": 100, "filterByFormula": formula}
    out = []; offset = None
    while True:
        if offset: params["offset"] = offset
        r = airtable_request("GET", url, HEADERS, params=params)
        j = r.json(); out.extend(j.get("records", [])); offset = j.get("offset"); time.sleep(0.25)
        if not offset: break
    return out


def _oldest_first(recs):
    groups = collections.defaultdict(list)
    for r in recs:
        groups[(r.get("fields", {}).get(F_CHECKED) or "")].append(r)
    ordered = []
    for day in sorted(groups):            # "" (never visited) sorts before any ISO date
        batch = groups[day]
        random.shuffle(batch)             # avoid hammering one domain in a burst
        ordered.extend(batch)
    return ordered


def order_targets(recs, max_pages):
    """Rotating queue, oldest-first, with batch composition control. The queue has two pools:
    PRODUCTIVE pages (needs_browser and readable: JS sites that actually yield documents) and
    RESCUE pages (status dead/error: bot-walls and dead links, each burning the full nav
    timeout). Oldest-first alone let the rescue pool monopolise whole batches with the slowest
    possible pages; now rescues are capped at RESCUE_SHARE of the batch and productive pages
    fill the rest, so JS companies get guaranteed daily throughput whatever the wall pool does.
    Returns (batch, remaining)."""
    rescue = [r for r in recs if (r.get("fields", {}).get(F_STATUS)) in ("dead", "error")]
    productive = [r for r in recs if r not in rescue] if len(recs) < 5000 else None
    if productive is None:   # avoid O(n^2) membership on big queues
        rescue_ids = {r["id"] for r in rescue}
        productive = [r for r in recs if r["id"] not in rescue_ids]
    productive = _oldest_first(productive); rescue = _oldest_first(rescue)
    if not max_pages or max_pages <= 0:
        ordered = productive + rescue
        return ordered, 0
    r_cap = int(max_pages * RESCUE_SHARE)
    batch = productive[:max_pages - r_cap] + rescue[:r_cap]
    # backfill if one pool is short
    if len(batch) < max_pages:
        batch += (productive[max_pages - r_cap:] + rescue[r_cap:])[:max_pages - len(batch)]
    remaining = len(recs) - len(batch)
    return batch, max(remaining, 0)


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
    """One Playwright+Chromium per worker thread, RELAUNCHED every RECYCLE_EVERY pages:
    long-lived Chromium instances degrade over hundreds of renders, and a fresh launch is
    cheaper than the slow creep."""
    n = getattr(_tl, "count", 0)
    if n and n % RECYCLE_EVERY == 0 and hasattr(_tl, "ctx"):
        try:
            _tl.browser.close(); _tl.pw.stop()
        except Exception:
            pass
        del _tl.ctx
    _tl.count = n + 1
    if not hasattr(_tl, "ctx"):
        _tl.pw = sync_playwright().start()
        _tl.browser = _tl.pw.chromium.launch(headless=True)
        _tl.ctx = _tl.browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900})
        _tl.ctx.set_default_timeout(NAV_TIMEOUT_MS)
        _tl.ctx.route(re.compile(r"\.(png|jpe?g|gif|webp|svg|ico|woff2?|ttf|mp4|webm)(\?|$)", re.I),
                      lambda route: route.abort())
    return _tl.ctx


def process(rec):
    f = rec.get("fields", {}); u = f.get(F_URL, "")
    upd = {F_CHECKED: TODAY.isoformat()}
    page = None
    t0 = time.time()
    try:
        page = get_page().new_page()
        resp = page.goto(u, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(SETTLE_MS)
        code = resp.status if resp else None
        if code and code >= 400:
            upd[F_STATUS] = "dead"; upd[F_HTTP] = str(code)
            return rec["id"], upd, False, False, [], False, time.time() - t0
        final = page.url
        upd[F_HTTP] = str(code or "")
        upd[F_FINAL] = final
        upd[F_STATUS] = "redirected" if normalize(final) != normalize(u) else "ok"
        current = doc_links(page.content(), final)
        had_baseline = bool(f.get(F_HASH))          # content_hash present = visited before
        changed, high, docs = detection_fields(f, upd, current, had_baseline, TODAY)
        if OWN_PAGES or f.get(F_BROWSER):
            upd[F_BROWSER] = True                    # browser owns this page from now on
        return rec["id"], upd, changed, high, docs, True, time.time() - t0
    except Exception:
        upd[F_STATUS] = "error"
        return rec["id"], upd, False, False, [], False, time.time() - t0
    finally:
        if page:
            try: page.close()
            except Exception: pass


def main():
    global OWN_PAGES
    ap = argparse.ArgumentParser()
    ap.add_argument("--wba", default=os.environ.get("PW_WBA", ""), help="comma-separated wba_ids: render ALL their monitored URLs")
    ap.add_argument("--contains", default=os.environ.get("PW_CONTAINS", ""), help="substring filter on url (case-insensitive)")
    ap.add_argument("--max-pages", type=int, default=int(os.environ.get("MAX_PAGES", "0") or 0),
                    help="cap pages this run (0 = no cap); the queue rotates, next run continues")
    ap.add_argument("--workers", type=int, default=int(os.environ.get("PW_WORKERS", "4") or 4))
    ap.add_argument("--budget-min", type=int, default=int(os.environ.get("RUN_BUDGET_MIN", "180") or 180),
                    help="wall-clock budget; the run always finishes cleanly inside it (0 = off)")
    args = ap.parse_args()

    targeted = bool(args.wba.strip() or args.contains.strip())
    OWN_PAGES = not targeted
    formula = build_formula(args.wba, args.contains)
    print(f"targets: {formula}")
    recs = get_targets(formula)
    recs, remaining = order_targets(recs, 0 if targeted else args.max_pages)
    mode = "TARGETED (all monitored rows of the target; not claiming pages)" if targeted \
           else f"queue, oldest-first (cap {args.max_pages or 'none'})"
    print(f"{len(recs)} pages to render with a real browser ({args.workers} workers). Mode: {mode}.")

    budget_s = args.budget_min * 60
    t0 = time.time()
    updates = []; rescued = changed = still_dead = 0; done = 0; alerted = 0; slow = 0
    stopped_early = False
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        i = 0
        CHUNK = max(args.workers * 10, 20)
        while i < len(recs):
            if budget_s and time.time() - t0 > budget_s:
                stopped_early = True
                print(f">>> time budget ({args.budget_min} min) reached with {len(recs) - i} "
                      f"pages unprocessed this run; they stay at the head of the rotation.", flush=True)
                break
            chunk = recs[i:i + CHUNK]; i += len(chunk)
            for fut in as_completed([ex.submit(process, r) for r in chunk]):
                rid, upd, ch, high, _docs, ok, secs = fut.result()
                if ok: rescued += 1
                elif upd.get(F_STATUS) in ("dead", "error"): still_dead += 1
                if ch: changed += 1
                if high: alerted += 1
                if secs > 30:
                    slow += 1
                    print(f"    SLOW {secs:.0f}s  {upd.get(F_FINAL) or ''}", flush=True)
                updates.append({"id": rid, "fields": upd}); done += 1
                if len(updates) >= 100:    # checkpoint progress so a long run survives a hiccup
                    patch(updates); updates = []
            if done % 200 < CHUNK: print(f"  {done}/{len(recs)}  ({(time.time()-t0)/60:.0f} min)", flush=True)
    print(f"Writing the final {len(updates)} updates back to Airtable ...")
    patch(updates)
    print(f"Done. Readable in a real browser: {rescued}.  Still dead/error: {still_dead}.  "
          f"Pages with new doc links: {changed} (high-signal alerts: {alerted}).")
    left = remaining + (len(recs) - done)
    if left or stopped_early:
        print(f"Queue not fully covered this run: {left} pages left "
              f"(cap {remaining}, budget {len(recs) - done}); rotation continues next run.")
    if slow:
        print(f"Slow pages (>30s): {slow} — listed above; recurring hosts are stealth-wall candidates.")


if __name__ == "__main__":
    main()
