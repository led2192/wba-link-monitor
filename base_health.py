#!/usr/bin/env python3
"""
base_health.py — one row per day in `base_health`: is the base healthy, and how complete is it?

Built so one person can run the base without opening GitHub every day. It reads, it never
changes data, and it writes a single row (upserted by date) with:
  - how full the base is against the Airtable record limit (RECORD_LIMIT, default 500,000)
  - what came in (report_library rows created in the last 24h) and what was discarded, by reason
  - how many discarded rows the purge job could delete today (discarded > PURGE_DAYS ago)
  - which GitHub workflow runs failed in the last 24h, with links
  - company completeness: the average of companies.completeness (written by company_coverage.py),
    the share of companies at 100%, and how many companies miss each of the six checks
  - status (OK / Check / Problem) and `attention`: the plain-language list of what needs a person

Status rules: Problem when a job failed or the base is >= 90% full. Check when the base is >= 80%
full, nothing new arrived in 24h, the purge backlog is large, or average completeness dropped by
more than one point since the previous row. OK otherwise.

Modes: dry run by default (prints the row); --commit writes it.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE, RECORD_LIMIT, PURGE_DAYS (default 30),
     GITHUB_TOKEN + GITHUB_REPOSITORY (optional, for failed jobs; set by Actions).
"""
import argparse, collections, os, sys, time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

API = "https://api.airtable.com/v0"
TOKEN = os.environ.get("AIRTABLE_TOKEN"); BASE = os.environ.get("AIRTABLE_BASE")
H = {"Authorization": f"Bearer {TOKEN}"}
RECORD_LIMIT = int(os.environ.get("RECORD_LIMIT") or 500_000)
PURGE_DAYS = int(os.environ.get("PURGE_DAYS") or 30)
HEALTH = "base_health"
# Every table counts toward the base limit. One cheap field per table is enough to count rows.
TABLES = {"report_library": None, "monitored_links": None, "webpage_library": None,
          "companies": None, "mention_searches": None, "mention_hits": None, "base_health": None}
CHECKS = [("Report hub page", "missing_report_hub"), ("Sustainability page", "missing_sust_page"),
          ("Sustainability report (recent)", "missing_sust_report"),
          ("Annual report (recent)", "missing_annual_report"),
          ("Code of Conduct", "missing_code_of_conduct"),
          ("Human Rights Policy", "missing_human_rights_policy")]


def req(method, url, **kw):
    delay = 2
    for attempt in range(6):
        try:
            r = requests.request(method, url, headers=H, timeout=60, **kw)
        except requests.exceptions.RequestException:
            if attempt == 5: raise
            time.sleep(delay); delay = min(delay * 2, 30); continue
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == 5: r.raise_for_status()
            time.sleep(delay); delay = min(delay * 2, 30); continue
        r.raise_for_status()
        return r


def sweep(table, fields, formula=None):
    url = f"{API}/{BASE}/{quote(table)}"
    params = [("pageSize", "100")] + [("fields[]", f) for f in fields]
    if formula:
        params.append(("filterByFormula", formula))
    offset = None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = req("GET", url, params=p).json()
        yield from j.get("records", [])
        offset = j.get("offset"); time.sleep(0.12)
        if not offset:
            break


def parse_ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def library_stats(now):
    s = {"live": 0, "discarded": 0, "purge_ready": 0, "new_24h": 0, "disc_24h": collections.Counter(),
         "rows": 0}
    day_ago, purge_cut = now - timedelta(days=1), now - timedelta(days=PURGE_DAYS)
    for rec in sweep("report_library", ["discard", "discard_reason", "discarded_on", "verified"]):
        s["rows"] += 1
        f = rec.get("fields", {})
        ct = parse_ts(rec.get("createdTime"))
        if ct and ct >= day_ago:
            s["new_24h"] += 1
        if f.get("discard"):
            s["discarded"] += 1
            d = parse_ts(f.get("discarded_on"))
            if d and d >= day_ago:
                s["disc_24h"][f.get("discard_reason") or "(no reason)"] += 1
            if d and d < purge_cut and not f.get("verified"):
                s["purge_ready"] += 1
        else:
            s["live"] += 1
    return s


def count_rows(table, field=None):
    n = 0
    try:
        for _ in sweep(table, [field] if field else []):
            n += 1
    except requests.HTTPError as e:
        print(f"    could not count {table}: {e}", flush=True)
        return 0
    return n


def company_stats():
    vals, miss, over = [], collections.Counter(), collections.Counter()
    for rec in sweep("companies", ["completeness", "gap_pages", "overdue_reports"]):
        f = rec.get("fields", {})
        vals.append(float(f.get("completeness") or 0))
        miss.update(f.get("gap_pages") or [])
        over.update(f.get("overdue_reports") or [])
    n = len(vals)
    return {"companies": n, "avg": (sum(vals) / n) if n else 0,
            "complete": (sum(1 for v in vals if v >= 0.9999) / n) if n else 0, "miss": miss,
            "over": over}


QA_TABLES = ("runs", "failures", "labels", "evals")


def qa_stats():
    """Rows in the Pipeline QA base (its own 500,000 cap), open failures, reviewed labels.
    None when AIRTABLE_QA_TOKEN is not set. Read-only."""
    tok = os.environ.get("AIRTABLE_QA_TOKEN"); base = os.environ.get("AIRTABLE_QA_BASE") or "appJQ2mRtjHjzLq9E"
    if not tok:
        return None
    h = {"Authorization": f"Bearer {tok}"}
    def count(table, formula=None):
        n, offset = 0, None
        params = [("pageSize", "100"), ("fields[]", "")]
        if formula:
            params.append(("filterByFormula", formula))
        while True:
            p = [x for x in params if x[1] != ""] + ([("offset", offset)] if offset else [])
            r = requests.get(f"{API}/{base}/{quote(table)}", headers=h, params=p, timeout=60)
            r.raise_for_status(); j = r.json()
            n += len(j.get("records", [])); offset = j.get("offset"); time.sleep(0.12)
            if not offset:
                return n
    try:
        per = {t: count(t) for t in QA_TABLES}
        return {"total": sum(per.values()), "per": per,
                "open": count("failures", "{status}='open'"),
                "reviewed": count("labels", "AND({reviewed}, {sure})")}
    except requests.RequestException as e:
        print(f"    could not read Pipeline QA: {e}", flush=True)
        return None


def failed_jobs(now):
    tok, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (tok and repo):
        return None
    since = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.get(f"https://api.github.com/repos/{repo}/actions/runs",
                     headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"},
                     params={"created": f">={since}", "per_page": 100}, timeout=60)
    r.raise_for_status()
    bad = [w for w in r.json().get("workflow_runs", [])
           if w.get("conclusion") in ("failure", "timed_out", "startup_failure")]
    return [f"{w['name']} ({w['created_at'][:16].replace('T', ' ')} UTC): {w['html_url']}" for w in bad]


def previous_avg(today):
    rows = list(sweep(HEALTH, ["date", "completeness_avg"], formula=f"IS_BEFORE({{date}}, '{today}')"))
    rows.sort(key=lambda r: r.get("fields", {}).get("date", ""))
    return rows[-1]["fields"].get("completeness_avg") if rows else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    if not (TOKEN and BASE):
        sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
    now = datetime.now(timezone.utc); today = now.date().isoformat()

    print(">>> report_library ...", flush=True)
    lib = library_stats(now)
    print(">>> other tables ...", flush=True)
    counts = {"report_library": lib["rows"]}
    for t in TABLES:
        if t != "report_library":
            counts[t] = count_rows(t)
    pages_mon = sum(1 for _ in sweep("monitored_links", ["monitor"], formula="{monitor}"))
    pages_check = sum(1 for _ in sweep("monitored_links", ["monitor"],
                                       formula="{possible_issue}='Document links not in the library'"))
    print(">>> companies ...", flush=True)
    co = company_stats()
    fails = failed_jobs(now)
    prev = previous_avg(today)
    qs = qa_stats()

    total = sum(counts.values())
    full = total / RECORD_LIMIT
    attention, status = [], "OK"
    if fails:
        status = "Problem"
        attention.append(f"{len(fails)} GitHub job(s) failed in the last 24h. Open the links in failed_jobs.")
    if full >= 0.9:
        status = "Problem"
        attention.append(f"The base is {full:.0%} full. Run 'Purge discarded rows' now "
                         f"({lib['purge_ready']:,} rows ready).")
    elif full >= 0.8:
        status = "Check" if status == "OK" else status
        attention.append(f"The base is {full:.0%} full. Plan a purge ({lib['purge_ready']:,} rows ready).")
    if lib["purge_ready"] >= 10_000 and full < 0.8:
        status = "Check" if status == "OK" else status
        attention.append(f"{lib['purge_ready']:,} discarded rows are ready to purge.")
    if lib["new_24h"] == 0:
        status = "Check" if status == "OK" else status
        attention.append("Nothing new arrived in 24h. Check that the monitor and harvest ran.")
    if prev is not None and co["avg"] < float(prev) - 0.01:
        status = "Check" if status == "OK" else status
        attention.append(f"Average completeness fell from {float(prev):.1%} to {co['avg']:.1%}. "
                         "Look at which gap counts went up.")
    if fails is None:
        attention.append("Failed-job check skipped (no GitHub token in this run).")
    if qs:
        qfull = qs["total"] / RECORD_LIMIT
        if qfull >= 0.9:
            status = "Problem"
            attention.append(f"Pipeline QA is {qfull:.0%} full. Run 'QA archive' now.")
        elif qfull >= 0.8:
            status = "Check" if status == "OK" else status
            attention.append(f"Pipeline QA is {qfull:.0%} full. Run 'QA archive' (commit) this week.")

    row = {"date": today, "status": status, "attention": "\n".join(attention) or "Nothing needs you.",
           "records_total": total, "record_limit": RECORD_LIMIT, "base_full": round(full, 4),
           "docs_live": lib["live"], "docs_discarded": lib["discarded"], "purge_ready": lib["purge_ready"],
           "pages_monitored": pages_mon, "new_docs_24h": lib["new_24h"],
           "discarded_24h": sum(lib["disc_24h"].values()),
           "discarded_24h_by_reason": "\n".join(f"{k}: {v:,}" for k, v in lib["disc_24h"].most_common()),
           "failed_jobs_24h": len(fails or []), "failed_jobs": "\n".join(fails or []),
           "companies": co["companies"], "completeness_avg": round(co["avg"], 4),
           "companies_complete": round(co["complete"], 4),
           "overdue_sust_reports": co["over"].get("Sustainability report", 0),
           "overdue_annual_reports": co["over"].get("Annual report", 0),
           "pages_to_check": pages_check}
    if qs:
        row.update({"qa_records_total": qs["total"], "qa_base_full": round(qs["total"] / RECORD_LIMIT, 4),
                    "qa_open_failures": qs["open"], "qa_labels_reviewed": qs["reviewed"]})
    for tag, field in CHECKS:
        row[field] = co["miss"].get(tag, 0)

    print("\n".join(f"  {k}: {v}" for k, v in row.items()), flush=True)
    print("  records by table: " + ", ".join(f"{t} {n:,}" for t, n in counts.items()), flush=True)
    if not a.commit:
        print(">>> dry run, nothing written.")
        return
    url = f"{API}/{BASE}/{quote(HEALTH)}"
    existing = list(sweep(HEALTH, ["date"], formula=f"IS_SAME({{date}}, '{today}', 'day')"))
    if existing:
        req("PATCH", url, json={"records": [{"id": existing[0]["id"], "fields": row}], "typecast": True})
    else:
        req("POST", url, json={"records": [{"fields": row}], "typecast": True})
    print(f">>> base_health row for {today} written: {status}.")


if __name__ == "__main__":
    main()
