#!/usr/bin/env python3
"""
page_scans.py: the scans people ask for with the browser extension ("Scan this page").

The extension writes a request into public.page_scans (function scan_page in supabase/search.sql). This script,
run by the Scan now watch at every check, does two things:
  1. a new request: when the page is already watched for that company, its scan_now box is ticked in
     monitored_links (status queued); when it is not, the page is handed to the company's inbox through
     public.source_submissions, as a link added by a person (status added). The watch then reads it.
  2. a request on its way: when the page's scan has finished (scan_now unticked and a scan_status that is not
     the one seen when the scan was asked), the result is copied to the request (status done) for the
     extension to show. A request with no result after three hours is closed as failed and recorded.

Dry run by default; --commit writes. It writes two boxes in Airtable only: scan_now on a monitored page, as a
person ticking it does. Deterministic and repeatable: a request is taken once (its status changes with the write).
Env: SUPABASE_DB_URL, AIRTABLE_TOKEN, AIRTABLE_BASE.
"""
import argparse, os, sys
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urlsplit

GIVE_UP = timedelta(hours=3)
FIELDS = ["wba_id", "url", "scan_now", "scan_status", "last_checked", "monitor"]


def norm(url):
    """One spelling for one page: no fragment, no trailing slash, host in lower case without www."""
    u = (url or "").strip().split("#", 1)[0]
    p = urlsplit(u)
    host = p.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    path = p.path.rstrip("/")
    return f"{host}{path}" + (f"?{p.query}" if p.query else "")


def plan(requests, links):
    """What to do with the new requests. requests: [(id, wba_id, url)]; links: monitored_links records.
    Returns (ticks, adds): ticks = [(request id, record id, scan_status now)] for pages already watched for that
    company; adds = [(request id, wba_id, url)] for pages lanterN does not watch yet."""
    by_page = {}
    for r in links:
        f = r.get("fields", {})
        by_page.setdefault(((f.get("wba_id") or "").strip(), norm(f.get("url"))), r)
    ticks, adds = [], []
    for rid, wba, url in requests:
        rec = by_page.get((wba, norm(url)))
        if rec:
            ticks.append((rid, rec["id"], rec.get("fields", {}).get("scan_status") or ""))
        else:
            adds.append((rid, wba, url))
    return ticks, adds


def settle(open_requests, links, now):
    """Which requests on their way are finished. open_requests: [(id, wba_id, url, status, link_record, before, at)].
    Returns (done, found, failed): done = [(id, result)]; found = [(id, record id)] for an added page that now has
    its row; failed = [id] with no result after GIVE_UP."""
    by_id = {r["id"]: r for r in links}
    by_page = {}
    for r in links:
        f = r.get("fields", {})
        by_page.setdefault(((f.get("wba_id") or "").strip(), norm(f.get("url"))), r)
    done, found, failed = [], [], []
    for rid, wba, url, status, link_record, before, at in open_requests:
        rec = by_id.get(link_record) if link_record else by_page.get((wba, norm(url)))
        if rec:
            f = rec.get("fields", {})
            text = (f.get("scan_status") or "").strip()
            finished = bool(text) and not f.get("scan_now") and not text.startswith("Scanning since") and text != (before or "").strip()
            if finished:
                done.append((rid, text[:600]))
                continue
            if not link_record:
                found.append((rid, rec["id"]))
        if now - at > GIVE_UP:
            failed.append(rid)
    return done, found, failed


def links_of(companies):
    """The monitored_links rows of these companies."""
    from monitor_core import airtable_request
    base, token = os.environ["AIRTABLE_BASE"], os.environ["AIRTABLE_TOKEN"]
    url = f"https://api.airtable.com/v0/{base}/{quote('monitored_links')}"
    out, ids = [], sorted(companies)
    for i in range(0, len(ids), 25):
        formula = "OR(" + ",".join("{wba_id}='%s'" % w.replace("'", "") for w in ids[i:i + 25]) + ")"
        params, offset = {"pageSize": 100, "filterByFormula": formula, "fields[]": FIELDS}, None
        while True:
            if offset:
                params["offset"] = offset
            j = airtable_request("GET", url, {"Authorization": f"Bearer {token}"}, params=params).json()
            out += j.get("records", [])
            offset = j.get("offset")
            if not offset:
                break
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args(argv)
    for v in ("SUPABASE_DB_URL", "AIRTABLE_TOKEN", "AIRTABLE_BASE"):
        if not os.environ.get(v):
            sys.exit(f"Set {v}.")
    import evidence_db
    with evidence_db.connect() as conn:
        cur = conn.cursor()
        cur.execute("select to_regclass('public.page_scans') is not null")
        if not cur.fetchone()[0]:
            print("public.page_scans is not there yet: nothing to do.")
            return 0
        cur.execute("select id, wba_id, url, status, link_record, before, at from public.page_scans "
                    "where status in ('new', 'queued', 'added') order by id limit 200")
        rows = cur.fetchall()
        if not rows:
            return 0                                   # the usual case, every three minutes: say nothing
        import qa
        from monitor_core import airtable_request
        if a.commit:
            os.environ["QA_COMMIT"] = "true"
        run = qa.Run("page_scans.py")
        links = links_of({r[1] for r in rows})
        now = datetime.now(timezone.utc)
        ticks, adds = plan([(r[0], r[1], r[2]) for r in rows if r[3] == "new"], links)
        done, found, failed = settle([r for r in rows if r[3] != "new"], links, now)
        print(f"{len(rows)} scan requests open: {len(ticks)} pages to scan now, {len(adds)} new pages to add, "
              f"{len(done)} finished, {len(failed)} with no result after {int(GIVE_UP.total_seconds() // 3600)} hours", flush=True)
        for rid in failed:
            run.failure("DATA_CHECK", "a scan asked from the browser had no result after three hours", key=f"page_scan|{rid}")
        if a.commit:
            base, token = os.environ["AIRTABLE_BASE"], os.environ["AIRTABLE_TOKEN"]
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            api = f"https://api.airtable.com/v0/{base}/{quote('monitored_links')}"
            for rid, rec, before in ticks:
                airtable_request("PATCH", f"{api}/{rec}", headers, payload={"fields": {"scan_now": True}})
                cur.execute("update public.page_scans set status = 'queued', link_record = %s, before = %s, result = 'waiting for the scan' "
                            "where id = %s and status = 'new'", (rec, before, rid))
                conn.commit()                          # one by one: a failure later leaves the earlier ones consistent
            for rid, wba, url in adds:
                cur.execute("insert into public.source_submissions (reviewer, wba_id, url, client) "
                            "select reviewer, wba_id, url, client from public.page_scans where id = %s "
                            "on conflict on constraint source_submissions_one_per_person do update set status = 'new', result = null", (rid,))
                cur.execute("update public.page_scans set status = 'added', result = 'a new page: on its way into the library' "
                            "where id = %s and status = 'new'", (rid,))
                conn.commit()
            if adds:                                   # into the companies' inboxes now, not at the hourly job
                import subprocess
                try:
                    subprocess.run([sys.executable, "source_submissions.py", "--commit"], timeout=600)
                except Exception as e:
                    print(f">>> source_submissions failed ({type(e).__name__}); the hourly job will pass the pages on.", flush=True)
            for rid, rec in found:
                cur.execute("update public.page_scans set link_record = %s where id = %s", (rec, rid))
            for rid, text in done:
                cur.execute("update public.page_scans set status = 'done', result = %s, done_at = now() where id = %s", (text, rid))
            for rid in failed:
                cur.execute("update public.page_scans set status = 'failed', result = 'no result after three hours', done_at = now() where id = %s", (rid,))
            conn.commit()
        run.finish("ok", rows_in=len(rows), rows_out=(len(ticks) + len(adds) + len(done) + len(failed)) if a.commit else 0,
                   counters={"open": len(rows), "ticked": len(ticks), "added": len(adds), "done": len(done), "failed": len(failed)},
                   notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
