#!/usr/bin/env python3
"""One-time (re-runnable) logo fetch: attach a company logo to companies.logo.

How: for each company, derive its primary web domain from its monitored pages
(the homepage row when present, otherwise the most common registrable domain
across its monitored URLs), and write an attachment-by-URL pointing at a favicon
service. Airtable downloads the image asynchronously, like report attachments.

Contract (house rules): idempotent (only rows whose logo field is empty),
dry-run by default, --commit writes, orphan-free (reads only, then patches
companies by record id). Provider swappable via LOGO_URL_TEMPLATE.

Env: AIRTABLE_TOKEN, AIRTABLE_BASE,
     LOGO_FIELD (default "logo"),
     LOGO_URL_TEMPLATE (default Google favicon service at 128px).
"""
import argparse, collections, os, time
from urllib.parse import quote
from monitor_core import airtable_request, reg_domain

API = "https://api.airtable.com/v0"
COMPANIES = "companies"
LINKS = "monitored_links"
F_WBA = "wba_id"
F_URL = "url"
F_TYPE = "type"
F_LOGO = os.environ.get("LOGO_FIELD", "logo")
TEMPLATE = os.environ.get(
    "LOGO_URL_TEMPLATE",
    "https://www.google.com/s2/favicons?domain={domain}&sz=128")


def sweep(base, token, table, fields, formula=None):
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}"}
    params = [("pageSize", "100")] + [("fields[]", f) for f in fields]
    if formula:
        params.append(("filterByFormula", formula))
    offset = None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", url, headers, params=p).json()
        yield from j.get("records", [])
        offset = j.get("offset")
        if not offset:
            return
        time.sleep(0.18)


def pick_domains(link_rows):
    """Pure: monitored_links rows -> {wba: domain}. Homepage row wins; otherwise the
    most common registrable domain across the company's monitored URLs."""
    home, counts = {}, collections.defaultdict(collections.Counter)
    for r in link_rows:
        f = r.get("fields", {})
        wba = (f.get(F_WBA) or "").strip()
        d = reg_domain(f.get(F_URL) or "")
        if not wba or not d:
            continue
        if (f.get(F_TYPE) or "") == "homepage" and wba not in home:
            home[wba] = d
        counts[wba][d] += 1
    out = dict(home)
    for wba, c in counts.items():
        out.setdefault(wba, c.most_common(1)[0][0])
    return out


def build_batches(companies_rows, domains, template, logo_field):
    """Pure: (companies rows missing a logo, {wba: domain}) -> (batches, no_domain)."""
    updates, no_domain = [], []
    for r in companies_rows:
        wba = (r.get("fields", {}).get(F_WBA) or "").strip()
        d = domains.get(wba)
        if not d:
            if wba:
                no_domain.append(wba)
            continue
        updates.append({"id": r["id"], "fields": {
            logo_field: [{"url": template.format(domain=d), "filename": f"{d}.png"}]}})
    return [updates[i:i + 10] for i in range(0, len(updates), 10)], no_domain


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    base = os.environ["AIRTABLE_BASE"]
    token = os.environ["AIRTABLE_TOKEN"]

    links = list(sweep(base, token, LINKS, [F_WBA, F_URL, F_TYPE]))
    domains = pick_domains(links)
    print(f"domains derived for {len(domains)} companies")

    todo = list(sweep(base, token, COMPANIES, [F_WBA],
                      formula=f"{{{F_LOGO}}} = BLANK()"))
    print(f"companies without a logo: {len(todo)}")

    batches, no_domain = build_batches(todo, domains, TEMPLATE, F_LOGO)
    total = sum(len(b) for b in batches)
    print(f"to write: {total}; companies with no derivable domain: {len(no_domain)}")
    for w in no_domain[:10]:
        print("  no domain:", w)

    if not args.commit:
        print("DRY-RUN: nothing written. Re-run with --commit.")
        return

    url = f"{API}/{base}/{quote(COMPANIES)}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    done = 0
    for b in batches:
        airtable_request("PATCH", url, headers, {"records": b})
        done += len(b)
        if done % 200 < 10:
            print(f"  {done}/{total}", flush=True)
        time.sleep(0.21)
    print(f"DONE: {done} logo attachments queued; Airtable downloads them in the background.")


if __name__ == "__main__":
    main()
