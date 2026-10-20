#!/usr/bin/env python3
"""
discover_pages.py — propose the missing report pages, never add them.

For companies that miss a report hub or a sustainability page (or have no pages at all, i.e. a new
company with only a website), it visits the company's website: the home page and up to MAX_CRAWL
section pages one hop in, the same crawl as the monthly active search. Pages that look like the
missing type AND link to documents become suggestions, written as lines in companies.suggested_links:

    reports_hub | https://www.example.com/investors/reports | "Annual reports" - links to 14 documents

Nothing is added to monitored_links: the person deletes wrong lines and ticks approve_suggestions,
and inbox.py adds what is left. That keeps the record count under control (500,000 limit) and the
person in charge of what is watched.

Order: new companies first, then the highest queue_score. A company is searched again after
RECHECK_DAYS at most, and never while suggestions are waiting. At most --max companies per run.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE.  Options: --max N (default 150), --wba ids, --commit
"""
import argparse, collections, os, re, sys, time
from datetime import date, timedelta
from urllib.parse import quote, urlsplit

import requests

API = "https://api.airtable.com/v0"
TOKEN = os.environ.get("AIRTABLE_TOKEN"); BASE = os.environ.get("AIRTABLE_BASE")
H = {"Authorization": f"Bearer {TOKEN}"}
RECHECK_DAYS = 60
MAX_CRAWL = 25
WANT = {"Report hub page": "reports_hub", "Sustainability page": "sustainability_page"}
MAX_PER_TYPE = 2


def get(table, params):
    from monitor_core import airtable_request
    out = []; offset = None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", f"{API}/{BASE}/{quote(table)}", H, params=p).json()
        out.extend(j.get("records", [])); offset = j.get("offset")
        if not offset:
            return out
        time.sleep(0.12)


def candidates(limit, wbas=None):
    today = date.today()
    fields = ["wba_id", "company_name", "website", "gap_pages", "urls_total", "queue_score",
              "suggested_links", "suggestions_checked"]
    formula = ("AND({suggested_links}='', OR({urls_total}=0, FIND('Report hub page', {gap_pages}&''), "
               "FIND('Sustainability page', {gap_pages}&'')))")
    if wbas:
        formula = "OR(" + ",".join(f"{{wba_id}}='{w}'" for w in wbas) + ")"
    recs = get("companies", [("pageSize", "100"), ("filterByFormula", formula)] + [("fields[]", f) for f in fields])
    cut = (today - timedelta(days=RECHECK_DAYS)).isoformat()
    if not wbas:
        recs = [r for r in recs if (r["fields"].get("suggestions_checked") or "") < cut]
    recs.sort(key=lambda r: (bool(r["fields"].get("urls_total")), -(r["fields"].get("queue_score") or 0)))
    return recs[:limit]


def homepage_of(rec, pages):
    w = (rec["fields"].get("website") or "").strip()
    if w:
        return w if w.startswith("http") else "https://" + w
    for p in pages:
        if p["fields"].get("type") == "homepage" and p["fields"].get("url"):
            return p["fields"]["url"]
    hosts = collections.Counter(urlsplit(p["fields"].get("url", "")).netloc for p in pages if p["fields"].get("url"))
    if hosts:
        return "https://" + hosts.most_common(1)[0][0]
    return ""


def explore(AS, sess, home, want_types, have_keys):
    """[(type, url, why)] pages one hop from the home page that look like the wanted types and
    link to documents. Best first: more document links, then section-looking address."""
    r = AS.fetch(sess, home)
    if r is None or r.status_code >= 400:
        return None, (r.status_code if r is not None else "no response")
    links = AS.links_on(r.text, r.url)
    links.sort(key=lambda lt: 0 if AS.SECTION.search(lt[0] + " " + lt[1]) else 1)
    seen, todo = set(), []
    for u, txt in links:
        k, _ = AS.normalize(u)
        if not k or k in seen or k in have_keys:
            continue
        seen.add(k)
        if AS.classify(u, txt) in want_types or AS.SECTION.search(u + " " + txt):
            todo.append((u, txt))
        if len(todo) >= MAX_CRAWL:
            break
    found = []
    for u, txt in todo:
        pr = AS.fetch(sess, u)
        if pr is None or pr.status_code >= 400:
            continue
        typ = AS.classify(pr.url, txt)
        if typ not in want_types:
            continue
        k, clean = AS.normalize(pr.url)
        if not k or k in have_keys:
            continue
        ndocs = sum(1 for lk, _ in AS.links_on(pr.text, pr.url)
                    if urlsplit(lk).path.lower().endswith(".pdf") or AS.DOCISH.search(urlsplit(lk).path))
        # PDFs on another host (a CDN) are not in links_on (same-domain only): count raw .pdf hrefs too
        ndocs = max(ndocs, len(set(re.findall(r'href=["\'][^"\']+\.pdf', pr.text, re.I))))
        if ndocs == 0:
            continue
        have_keys.add(k)
        found.append((typ, clean, f'"{txt[:60]}" - links to {ndocs} document(s)', ndocs))
    out = []
    for t in want_types:
        best = sorted([f for f in found if f[0] == t], key=lambda f: -f[3])[:MAX_PER_TYPE]
        out += [(a, b, c) for a, b, c, _ in best]
    return out, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=150)
    ap.add_argument("--wba", default="")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    if not (TOKEN and BASE):
        sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
    import active_search as AS
    from monitor_core import airtable_request
    wbas = [w.strip() for w in a.wba.split(",") if w.strip()]
    recs = candidates(a.max, wbas)
    print(f">>> {len(recs)} companies to search for missing report pages.", flush=True)
    sess = requests.Session(); updates = []; n_sugg = 0
    for rec in recs:
        f = rec["fields"]; wba = f.get("wba_id", "")
        pages = get("monitored_links", [("pageSize", "100"), ("filterByFormula", f"{{wba_id}}='{wba}'"),
                                        ("fields[]", "url"), ("fields[]", "type")])
        have = {AS.normalize(p["fields"].get("url", ""))[0] for p in pages}
        gaps = set(f.get("gap_pages") or [])
        want = [t for g, t in WANT.items() if g in gaps] or list(WANT.values())
        if not pages:
            want = list(WANT.values()) + ["investor_relations", "policies"]
        home = homepage_of(rec, pages)
        upd = {"suggestions_checked": date.today().isoformat()}
        if not home:
            print(f"    {f.get('company_name')}: no website known.", flush=True)
            updates.append({"id": rec["id"], "fields": upd}); continue
        try:
            found, err = explore(AS, sess, home, want, have)
        except Exception as e:
            found, err = None, type(e).__name__
        if found:
            upd["suggested_links"] = "\n".join(f"{t} | {u} | {why}" for t, u, why in found)
            n_sugg += 1
        print(f"    {f.get('company_name')}: {len(found or [])} suggestion(s)"
              + (f" (home page did not open: {err})" if err else ""), flush=True)
        updates.append({"id": rec["id"], "fields": upd})
    print(f">>> suggestions for {n_sugg} of {len(recs)} companies.", flush=True)
    if not a.commit:
        print(">>> dry run, nothing written."); return
    url = f"{API}/{BASE}/companies"
    for i in range(0, len(updates), 10):
        airtable_request("PATCH", url, {**H, "Content-Type": "application/json"}, {"records": updates[i:i + 10]})
        time.sleep(0.2)
    if wbas or n_sugg:
        import subprocess
        touched = [r["fields"].get("wba_id") for r in recs if r["fields"].get("wba_id")]
        for i in range(0, len(touched), 40):
            subprocess.run([sys.executable, "company_coverage.py", "--commit", "--wba", ",".join(touched[i:i + 40])],
                           check=False)


if __name__ == "__main__":
    main()
