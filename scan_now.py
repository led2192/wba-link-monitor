#!/usr/bin/env python3
"""
scan_now.py — scan a page on demand: find its documents and run them through the whole pipeline.

Picks up monitored_links rows that either
  - have scan_now ticked (by a person, e.g. from the Base health interface), or
  - were created in the last 2 days, are monitored and have never been checked (a newly added URL)
and for each one, straight away instead of waiting for the nightly chain:
  1. reads the page (plain request; falls back to a real browser when the page needs one or shows
     no documents) and stores what it found, exactly like the nightly monitors
  2. harvests the documents into report_library (same rules: doc_id guard, purge tombstones)
  3. guesses a type from the URL, attaches the PDF, waits for Airtable to fetch it, reads the cover
     (cover_text, page_count, language), which is what the AI title and type fields work from
  4. runs the duplicate and non-disclosure clean-up for that company
  5. recomputes that company's coverage and completeness (gaps and gap tags update at once)
  6. writes the outcome in plain words to scan_status and unticks scan_now

Runs every 10 minutes from GitHub Actions (scan_now.yml), and only does work when a row is waiting.
At most SCAN_MAX pages per run (default 20); the rest wait for the next run.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE, SCAN_MAX.
"""
import os, subprocess, sys, time
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import quote

import requests

from monitor_core import airtable_request, F_SEEN, F_STATUS, F_HTTP, F_BROWSER
from ids import normalize

API = "https://api.airtable.com/v0"
TOKEN = os.environ.get("AIRTABLE_TOKEN"); BASE = os.environ.get("AIRTABLE_BASE")
H = {"Authorization": f"Bearer {TOKEN}"}
LINKS, LIB, COMP = "monitored_links", "report_library", "companies"
SCAN_MAX = int(os.environ.get("SCAN_MAX") or 20)
QUEUE = ("OR({scan_now}, AND({monitor}, {last_checked}=BLANK(), "
         "IS_AFTER(CREATED_TIME(), DATEADD(NOW(), -2, 'days'))))")
ATTACH_WAIT_S = 240
COVER_MAX = int(os.environ.get("SCAN_COVER_MAX") or 80)


def get(table, params):
    out = []; offset = None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", f"{API}/{BASE}/{quote(table)}", H, params=p).json()
        out.extend(j.get("records", [])); offset = j.get("offset")
        if not offset:
            return out
        time.sleep(0.12)


def patch(table, records):
    url = f"{API}/{BASE}/{quote(table)}"
    for i in range(0, len(records), 10):
        airtable_request("PATCH", url, {**H, "Content-Type": "application/json"},
                         {"records": records[i:i + 10], "typecast": True})
        time.sleep(0.2)


def queue():
    recs = get(LINKS, [("pageSize", "100"), ("filterByFormula", QUEUE)])
    recs.sort(key=lambda r: (not r["fields"].get("scan_now"), r.get("createdTime", "")))
    return recs[:SCAN_MAX], len(recs)


def resolve_company(recs):
    """Rows added through a form may carry only the companies link: fill wba_id and company_name."""
    need = [r for r in recs if not (r["fields"].get("wba_id") or "").strip() and r["fields"].get("companies")]
    if not need:
        return
    ids = sorted({r["fields"]["companies"][0] for r in need})
    comp = {c["id"]: c["fields"] for c in
            get(COMP, [("pageSize", "100"), ("fields[]", "wba_id"), ("fields[]", "company_name"),
                       ("filterByFormula", "OR(" + ",".join(f"RECORD_ID()='{i}'" for i in ids) + ")")])}
    fixes = []
    for r in need:
        c = comp.get(r["fields"]["companies"][0], {})
        if c.get("wba_id"):
            r["fields"]["wba_id"] = c["wba_id"]; r["fields"]["company_name"] = c.get("company_name", "")
            fixes.append({"id": r["id"], "fields": {"wba_id": c["wba_id"],
                                                    "company_name": c.get("company_name", "")}})
    patch(LINKS, fixes)


def n_docs(upd):
    return len([l for l in (upd.get(F_SEEN) or "").split("\n") if l.strip()])


def read_page(rec, sess):
    """Plain request first; a real browser when the page needs one or the plain read finds nothing."""
    import airtable_monitor as AM
    _, upd, *_ = AM.process(rec, sess)
    how = "plain request"
    weak = upd.get(F_STATUS) in ("dead", "error") or n_docs(upd) == 0 or rec["fields"].get(F_BROWSER)
    if weak:
        try:
            import playwright_monitor as PM
            _, bupd, *_ = PM.process(rec)
            if n_docs(bupd) > n_docs(upd) or (upd.get(F_STATUS) in ("dead", "error")
                                             and bupd.get(F_STATUS) not in ("dead", "error")):
                upd, how = bupd, "browser"
        except (Exception, SystemExit) as e:     # playwright missing exits at import
            print(f"    browser read failed for {rec['fields'].get('url')}: {e}", flush=True)
    return upd, how


PROBE_MAX = int(os.environ.get("SCAN_PROBE_MAX") or 60)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def probe_docs(seen, sess):
    """Report-ish links on the page that are not PDF-shaped (no .pdf, no known document endpoint):
    open the first bytes of each and keep the ones that really are PDFs. This is how a site with a
    new address scheme still gets harvested on demand. Returns the normalized keys confirmed."""
    import harvest_reports as HR, doc_probe as DP
    todo = [u for u in seen if not HR.is_pdf_url(u) and DP.priority(u) is not None][:PROBE_MAX]
    res = DP.probe(todo, workers=8, session=sess)
    return {k: t for k, (v, t) in res.items() if v == "pdf"}, len(todo)


def wait_for_files(ids):
    """Airtable fetches attachments in the background. Poll until every row has a file or gave up."""
    deadline = time.time() + ATTACH_WAIT_S
    got = {}
    while ids and time.time() < deadline:
        time.sleep(20)
        for k in range(0, len(ids), 40):          # keep the formula (a URL parameter) short
            f = "OR(" + ",".join(f"RECORD_ID()='{i}'" for i in ids[k:k + 40]) + ")"
            for r in get(LIB, [("pageSize", "100"), ("fields[]", "file"), ("filterByFormula", f)]):
                if r["fields"].get("file"):
                    got[r["id"]] = r
        ids = [i for i in ids if i not in got]
    return list(got.values())


def company_completeness(wba):
    rows = get(COMP, [("pageSize", "1"), ("fields[]", "completeness"), ("fields[]", "gaps"),
                      ("filterByFormula", f"{{wba_id}}='{wba}'")])
    if not rows:
        return None, ""
    f = rows[0]["fields"]
    return f.get("completeness"), (f.get("gaps") or "").split("\n")[0]


def main():
    if not (TOKEN and BASE):
        sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
    pages, waiting = queue()
    if not pages:
        print("Nothing to scan."); return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f">>> scanning {len(pages)} page(s) ({waiting} waiting).", flush=True)
    patch(LINKS, [{"id": r["id"], "fields": {"scan_status": f"Scanning since {stamp} ..."}} for r in pages])
    resolve_company(pages)

    import harvest_reports as HR, classify_library as CL
    sess = requests.Session()
    # A tick on scan_now by a person (not the automatic scan of a new page) is a suspicion that
    # something is wrong with the page: record it, so the weekly missed-reports audit reviews
    # manual scans that found nothing first (last_manual_scan + scan_outcome + scan_concern).
    manual = {r["id"] for r in pages if r["fields"].get("scan_now")}
    outcome = {}
    notes = {}; by_company = defaultdict(list); page_updates = []
    for rec in pages:
        f = rec["fields"]
        upd, how = read_page(rec, sess)
        upd["scan_now"] = False
        page_updates.append({"id": rec["id"], "fields": upd})
        merged = {**f, **upd}
        if upd.get(F_STATUS) in ("dead", "error"):
            outcome[rec["id"]] = "Page failed"
            notes[rec["id"]] = [f"The page could not be opened ({upd.get(F_HTTP) or 'no response'}). "
                                "Check the URL, or untick monitor if it is gone."]
            continue
        notes[rec["id"]] = [f"{n_docs(upd)} document link(s) on the page (read with a {how})."]
        typ = (f.get("type") or "").strip().lower()
        if typ not in HR.ALL_TYPES:
            outcome[rec["id"]] = "Row needs a fix"
            notes[rec["id"]].append(f"Page type '{typ or 'blank'}' is not harvested for documents. "
                                    "Set the type to reports_hub, sustainability_page, sustainability_report, "
                                    "investor_relations or policies and tick scan_now again.")
            continue
        if not f.get("wba_id"):
            outcome[rec["id"]] = "Row needs a fix"
            notes[rec["id"]].append("No company on this row: link it to a company and tick scan_now again.")
            continue
        seen = [l.strip() for l in (merged.get(F_SEEN) or "").split("\n") if l.strip()]
        confirmed, probed = probe_docs(seen, sess)
        if confirmed:
            HR.CONFIRMED_DOCS |= confirmed
            notes[rec["id"]].append(f"{len(confirmed)} of {probed} links without a .pdf ending turned "
                                    "out to be PDFs when opened, and are harvested too.")
        by_company[f["wba_id"]].append((rec, merged))
    patch(LINKS, page_updates)

    created_all = []
    for wba, items in by_company.items():
        rows, _, _ = HR.build([m for _, m in items], set(HR.ALL_TYPES), False)
        id_by_url = {normalize(m.get("url", "")): rec["id"] for rec, m in items}
        existing = HR.existing_library(BASE, TOKEN, LIB, formula=f"{{wba_id}}='{wba}'")
        created, n_upd, blocked = HR.upsert(rows, id_by_url, BASE, TOKEN, LIB, existing=existing)
        created_all.extend(created)
        per_page = defaultdict(int)
        for c in created:
            per_page[normalize(c["fields"].get("found_on", ""))] += 1
        for rec, m in items:
            n_new = per_page.get(normalize(m.get("url", "")), 0)
            outcome[rec["id"]] = "Found new documents" if n_new else "Nothing new"
            notes[rec["id"]].append(f"{n_new} new document(s) added to the library"
                                    + (f"; {blocked} skipped for the company as already held under "
                                       "another address" if blocked else "") + ".")

    if created_all:
        print(f">>> {len(created_all)} new library rows: type guess + attach ...", flush=True)
        upd = []
        for c in created_all:
            t, conf = CL.classify(c["fields"].get("document_url", ""))
            fields = {"file": [{"url": c["fields"].get("document_url", "")}]}
            if t:
                fields.update({"source_type": t, "match_confidence": conf})
            upd.append({"id": c["id"], "fields": fields})
        patch(LIB, upd)
        with_file = wait_for_files([c["id"] for c in created_all])
        print(f">>> {len(with_file)}/{len(created_all)} PDFs attached; reading covers ...", flush=True)
        if with_file:
            # Covers for up to COVER_MAX rows now; any beyond that (a big report hub) are left with
            # cover_status blank and the nightly Build cover text job picks them up.
            import build_cover_text as BC
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=6) as ex:
                results = list(ex.map(lambda r: BC.process_one(r, 6), with_file[:COVER_MAX]))
            BC.write_batch(results)
        att = {normalize(c["fields"].get("found_on", "")) for c in created_all}
        for rec, _ in [i for v in by_company.values() for i in v]:
            if normalize(rec["fields"].get("url", "")) in att:
                notes[rec["id"]].append(f"PDFs attached for {len(with_file)} of {len(created_all)} new "
                                        f"document(s) this run, covers read for "
                                        f"{min(len(with_file), COVER_MAX)}; the AI title and type fill "
                                        "in from the cover as usual, the rest tonight.")

    if by_company:
        import dedupe_library as DL
        formula = "OR(" + ",".join(f"{{wba_id}}='{w}'" for w in by_company) + ")"
        rows = DL.from_airtable(BASE, TOKEN, LIB, formula)
        dups, _ = DL.plan(rows)
        junk = DL.plan_junk(rows, {r["id"] for r, _, _, _ in dups})
        DL.commit(BASE, TOKEN, LIB, dups + junk)
        print(f">>> clean-up: {len(dups)} duplicates, {len(junk)} non-disclosures discarded.", flush=True)

    touched = sorted({p["fields"].get("wba_id") for p in pages if p["fields"].get("wba_id")})
    if touched:
        subprocess.run([sys.executable, "company_coverage.py", "--commit", "--wba", ",".join(touched)],
                       check=False)
    final = []
    for rec in pages:
        msg = [stamp + ":"] + notes.get(rec["id"], [])
        wba = rec["fields"].get("wba_id")
        if wba:
            comp, first_gap = company_completeness(wba)
            if comp is not None:
                msg.append(f"Company completeness now {float(comp):.0%}"
                           + (f". Next gap: {first_gap}" if first_gap else ". Nothing missing.") )
        fields = {"scan_status": " ".join(msg), "scan_now": False,
                  "scan_outcome": outcome.get(rec["id"], "Nothing new")}
        if rec["id"] in manual:
            fields["last_manual_scan"] = stamp[:10]
            if fields["scan_outcome"] != "Found new documents":
                msg.append("Marked for the weekly missed-reports audit; add what looks wrong in "
                           "scan_concern if you can.")
                fields["scan_status"] = " ".join(msg)
        final.append({"id": rec["id"], "fields": fields})
    patch(LINKS, final)
    print(">>> done.", flush=True)


if __name__ == "__main__":
    main()
