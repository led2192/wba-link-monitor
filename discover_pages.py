#!/usr/bin/env python3
"""
discover_pages.py — find the missing report pages, and replacements for pages that no longer exist.

For companies that miss a report hub or a sustainability page (or have no pages at all, i.e. a new
company with only a website), it visits the company's website: the home page and up to MAX_CRAWL
section pages one hop in, the same crawl as the monthly active search. Pages that look like the
missing type AND link to documents become suggestions, written as lines in companies.suggested_links:

    reports_hub | https://www.example.com/investors/reports | "Annual reports" - links to 14 documents

A clear winner is added without waiting for a person (since 23 Oct 2026: nobody reviews suggestions
reliably): one candidate of the type with 3+ document links, or a best candidate with at least twice
the document links of the next. It is written to suggested_links with approve_suggestions ticked, so
inbox.py adds it (deduplicated, tombstones respected) and scans it within minutes. Ambiguous
candidates stay as suggestions in suggested_links for a person or the weekly review.

Gone pages (since 23 Oct 2026): a monitored report page (reports hub, sustainability, investor
relations, policies) that answers 404 or 410 is replaced the same way. When a clear replacement is
added, or the company is still watched through another live page of that type, the gone page is
unticked (monitor off) with a dated note; when nothing is found the page is unticked with a note too
(a 404 read every night teaches nothing) and the company is searched again after RECHECK_DAYS.

Order: companies with gone pages, new companies, then the highest queue_score. A company is searched again after
RECHECK_DAYS at most, and never while suggestions are waiting. At most --max companies per run.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE.  Options: --max N (default 150), --max-gone N (default 100),
--wba ids, --commit. Writes discover_pages.md (counts), posted on a pull request touching this file.
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
GONE_TYPES = ("reports_hub", "sustainability_page", "investor_relations", "policies")
MIN_DOCS_AUTO = 3


def clear_winner(cands):
    """The candidate to add without a person, or None. cands: [(type, url, why, ndocs)] of one type."""
    cands = sorted(cands, key=lambda c: -c[3])
    if not cands or cands[0][3] < MIN_DOCS_AUTO:
        return None
    if len(cands) == 1 or cands[0][3] >= 2 * cands[1][3]:
        return cands[0]
    return None


def plan(found, want):
    """Split explore() results into (auto, ambiguous) lists of (type, url, why, ndocs)."""
    auto, amb = [], []
    for t in want:
        mine = [c for c in found if c[0] == t]
        w = clear_winner(mine)
        if w:
            auto.append(w)
        else:
            amb += mine
    return auto, amb


def is_gone(f):
    return (f.get("monitor") and f.get("status") == "dead"
            and str(f.get("http_status") or "").split(".")[0] in ("404", "410")
            and (f.get("type") or "") in GONE_TYPES)


def gone_note(today, code, replacement=None, other_live=False):
    code = str(code or "404").split(".")[0]
    if replacement:
        return f"{today}: gone ({code}); replaced by {replacement}"
    if other_live:
        return f"{today}: gone ({code}); the company is still watched through another page of this type"
    return f"{today}: gone ({code}); no replacement found on the company's site, monitoring stopped"


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
    """[(type, url, why, ndocs)] pages one hop from the home page that look like the wanted types and
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
        out += sorted([f for f in found if f[0] == t], key=lambda f: -f[3])[:MAX_PER_TYPE]
    return out, None


def gone_by_company(wbas=None):
    fields = ["wba_id", "url", "type", "status", "http_status", "monitor", "notes"]
    formula = "AND({monitor}, {status}='dead', OR({http_status}=404, {http_status}=410))"
    if wbas:
        formula = "AND(" + formula + ", OR(" + ",".join(f"{{wba_id}}='{w}'" for w in wbas) + "))"
    out = collections.defaultdict(list)
    for r in get("monitored_links", [("pageSize", "100"), ("filterByFormula", formula)] + [("fields[]", f) for f in fields]):
        if is_gone(r["fields"]) and r["fields"].get("wba_id"):
            out[r["fields"]["wba_id"]].append(r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=150)
    ap.add_argument("--max-gone", type=int, default=100)
    ap.add_argument("--wba", default="")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    if not (TOKEN and BASE):
        sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
    import active_search as AS
    from monitor_core import airtable_request
    wbas = [w.strip() for w in a.wba.split(",") if w.strip()]
    today = date.today().isoformat()
    gone = gone_by_company(wbas)
    recs = candidates(a.max, wbas)
    if gone:
        have_ids = {r["fields"].get("wba_id") for r in recs}
        extra = [w for w in gone if w not in have_ids][:a.max_gone]
        if extra:
            recs = get("companies", [("pageSize", "100"), ("filterByFormula",
                       "OR(" + ",".join(f"{{wba_id}}='{w}'" for w in extra[:100]) + ")")]
                       + [("fields[]", f) for f in ["wba_id", "company_name", "website", "gap_pages", "urls_total",
                                                     "queue_score", "suggested_links", "suggestions_checked"]]) + recs
    print(f">>> {len(recs)} companies to search ({len(gone)} with gone report pages).", flush=True)
    from inbox import load_page_tombstones
    dead_pages = load_page_tombstones()
    sess = requests.Session(); updates = []; page_updates = []
    n = collections.Counter()
    for rec in recs:
        f = rec["fields"]; wba = f.get("wba_id", "")
        pages = get("monitored_links", [("pageSize", "100"), ("filterByFormula", f"{{wba_id}}='{wba}'"),
                                        ("fields[]", "url"), ("fields[]", "type"), ("fields[]", "monitor"),
                                        ("fields[]", "status"), ("fields[]", "http_status")])
        have = {AS.normalize(p["fields"].get("url", ""))[0] for p in pages}
        # pages deleted by hand are never proposed again
        have |= {AS.normalize(u)[0] for k, u in dead_pages.items() if k.startswith(wba + "|") and u}
        gone_here = gone.get(wba, [])
        gone_ids = {g["id"] for g in gone_here}
        live_types = {p["fields"].get("type") for p in pages
                      if p["fields"].get("monitor") and p["id"] not in gone_ids and p["fields"].get("status") != "dead"}
        gone_types = {g["fields"].get("type") for g in gone_here}
        replace = [t for t in GONE_TYPES if t in gone_types and t not in live_types]
        gaps = set(f.get("gap_pages") or [])
        want = [t for g, t in WANT.items() if g in gaps]
        if not pages:
            want = list(WANT.values()) + ["investor_relations", "policies"]
        want = list(dict.fromkeys(replace + want))
        upd = {"suggestions_checked": today}
        found, err = [], None
        home = homepage_of(rec, pages)
        if want and home:
            try:
                found, err = explore(AS, sess, home, want, have)
            except Exception as e:
                found, err = None, type(e).__name__
        auto, amb = plan(found or [], want)
        pending = (f.get("suggested_links") or "").strip()
        if auto:
            upd["suggested_links"] = "\n".join(f"{t} | {u} | {why} (added automatically: clear best match)"
                                               for t, u, why, _ in auto)
            upd["approve_suggestions"] = True
            n["companies_auto"] += 1; n["pages_auto"] += len(auto)
        elif amb and not pending:
            upd["suggested_links"] = "\n".join(f"{t} | {u} | {why}" for t, u, why, _ in amb)
            n["companies_ambiguous"] += 1
        added = {t: u for t, u, _, _ in auto}
        for g in gone_here:
            gf = g["fields"]; t = gf.get("type")
            note = gone_note(today, gf.get("http_status"), replacement=added.get(t), other_live=t in live_types)
            page_updates.append({"id": g["id"], "fields": {"monitor": False,
                                 "notes": (note + "\n" + (gf.get("notes") or "")).strip()[:5000]}})
            n["gone_replaced" if added.get(t) else ("gone_other_live" if t in live_types else "gone_unreplaced")] += 1
        print(f"    {f.get('company_name')}: {len(auto)} added, {len(amb)} ambiguous, {len(gone_here)} gone"
              + (f" (home page did not open: {err})" if err else "") + ("" if home else " (no website known)"), flush=True)
        updates.append({"id": rec["id"], "fields": upd})
    md = ["### Suggest missing report pages" + ("" if a.commit else " (dry run)"), "",
          f"Companies searched: {len(recs):,}.", "",
          "| Outcome | Count |", "| --- | --- |",
          f"| companies with a page added automatically (clear best match) | {n['companies_auto']:,} |",
          f"| pages added automatically | {n['pages_auto']:,} |",
          f"| companies left with ambiguous suggestions | {n['companies_ambiguous']:,} |",
          f"| gone pages replaced | {n['gone_replaced']:,} |",
          f"| gone pages, company still watched through another page of the type | {n['gone_other_live']:,} |",
          f"| gone pages, no replacement found | {n['gone_unreplaced']:,} |", ""]
    open("discover_pages.md", "w").write("\n".join(md))
    print("\n".join(md), flush=True)
    if not a.commit:
        print(">>> dry run, nothing written."); return
    for table, rows in (("companies", updates), ("monitored_links", page_updates)):
        url = f"{API}/{BASE}/{table}"
        for i in range(0, len(rows), 10):
            airtable_request("PATCH", url, {**H, "Content-Type": "application/json"}, {"records": rows[i:i + 10]})
            time.sleep(0.2)
    # approved suggestions are added by inbox.py on the next Scan now run (every 30 minutes), which
    # also refreshes the company's coverage; companies with only ambiguous suggestions are refreshed here
    touched = [r["fields"].get("wba_id") for r in recs if r["fields"].get("wba_id")]
    if touched:
        import subprocess
        for i in range(0, len(touched), 40):
            subprocess.run([sys.executable, "company_coverage.py", "--commit", "--wba", ",".join(touched[i:i + 40])],
                           check=False)


if __name__ == "__main__":
    main()
