#!/usr/bin/env python3
"""
source_submissions.py: the links people add in lanterN, passed on to the pipeline's inbox.

In lanterN a signed-in person can give links to documents we do not hold ("+ Add a source"). They land in
the evidence database (public.source_submissions, status new; a link the library already holds is refused
there). This script takes the new ones and writes each company's links into its `add_link` box in the
companies table, one per line, exactly as a person would paste them. From there inbox.py (the Scan now
watcher, within minutes) does everything else with the harvester's own duplicate guards: a document becomes
a report_library row, attached, read and labelled; a web page becomes a watched page; what we already hold is
said so in add_result. Nothing is written to report_library from here.

A link is marked queued once its company's box holds it, so a rerun never pastes it twice (and a link already
waiting in the box is not repeated). A submission whose company is not in the companies table is marked
orphan and recorded as a failure: no row is invented for it.

    python source_submissions.py [--commit] [--limit 200]

Dry run prints what it would paste, by company. Env: SUPABASE_DB_URL, AIRTABLE_TOKEN, AIRTABLE_BASE;
AIRTABLE_QA_TOKEN for the run record (QA_COMMIT=true writes it).
"""
import argparse, collections, os, sys
from datetime import datetime, timezone


def lines_of(text):
    """The non-empty lines of an add_link box."""
    return [l.strip() for l in (text or "").splitlines() if l.strip()]


def plan(rows, companies):
    """What to paste. rows: [(id, wba_id, url)]; companies: {wba_id: (record_id, add_link text)}.
    Returns (pastes {record_id: (wba_id, new box text, [ids])}, queued_only [ids already waiting in the box], orphans [(id, wba_id)])."""
    by_co = collections.OrderedDict()
    for sid, wba, url in rows:
        by_co.setdefault(wba, []).append((sid, url.strip()))
    pastes, waiting, orphans = {}, [], []
    for wba, items in by_co.items():
        if wba not in companies:
            orphans += [(sid, wba) for sid, _ in items]
            continue
        rec, box = companies[wba]
        have = lines_of(box)
        seen, add, ids = set(have), [], []
        for sid, url in items:
            if url in seen:
                waiting.append(sid)                      # already in the box (or twice in this batch): nothing to paste
                continue
            seen.add(url); add.append(url); ids.append(sid)
        if add:
            pastes[rec] = (wba, "\n".join(have + add), ids)
    return pastes, waiting, orphans


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=200, help="submissions this run")
    a = ap.parse_args()
    for v in ("SUPABASE_DB_URL", "AIRTABLE_TOKEN", "AIRTABLE_BASE"):
        if not os.environ.get(v):
            sys.exit(f"Set {v}.")
    import qa, evidence_db
    from urllib.parse import quote
    from base_health import sweep, API
    BASE = os.environ["AIRTABLE_BASE"]
    from monitor_core import airtable_request
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("source_submissions.py")
    with evidence_db.connect() as conn:
        cur = conn.cursor()
        cur.execute("select to_regclass('public.source_submissions') is not null")
        if not cur.fetchone()[0]:
            print("public.source_submissions is not there yet: nothing to do.")
            run.finish("ok", rows_in=0, rows_out=0, notes="no table yet")
            return 0
        cur.execute("select id, wba_id, url from public.source_submissions where status = 'new' order by id limit %s", (a.limit,))
        rows = cur.fetchall()
        if not rows:
            print("No new source to pass on.")
            run.finish("ok", rows_in=0, rows_out=0, notes="nothing new")
            return 0
        wanted = {r[1] for r in rows}
        companies = {}
        for rec in sweep("companies", ["wba_id", "add_link"]):
            f = rec.get("fields", {})
            w = (f.get("wba_id") or "").strip()
            if w in wanted:
                companies[w] = (rec["id"], f.get("add_link") or "")
        pastes, waiting, orphans = plan(rows, companies)
        n_links = sum(len(ids) for _, _, ids in pastes.values())
        print(f"{len(rows)} new submissions: {n_links} links to paste for {len(pastes)} companies, {len(waiting)} already waiting in a box, {len(orphans)} without a company row", flush=True)
        for rec, (wba, text, ids) in pastes.items():
            print(f"  {wba}: {len(ids)} link(s)", flush=True)
        for sid, wba in orphans:
            run.failure("DATA_CHECK", f"a source was added for {wba}, which is not in the companies table", key=f"source_submission|{sid}")
        if a.commit:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            headers = {"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}", "Content-Type": "application/json"}
            for rec, (wba, text, ids) in pastes.items():
                airtable_request("PATCH", f"{API}/{BASE}/{quote('companies')}/{rec}", headers, payload={"fields": {"add_link": text}})
                cur.execute("update public.source_submissions set status = 'queued', result = %s where id = any(%s) and status = 'new'",
                            (f"pasted into the company's inbox {now}", ids))
                conn.commit()                              # company by company: a failure later leaves the earlier ones consistent
            if waiting:
                cur.execute("update public.source_submissions set status = 'queued', result = %s where id = any(%s) and status = 'new'",
                            (f"already waiting in the company's inbox {now}", waiting))
            if orphans:
                cur.execute("update public.source_submissions set status = 'orphan', result = 'the company is not in the companies table' where id = any(%s) and status = 'new'",
                            ([sid for sid, _ in orphans],))
            conn.commit()
    run.finish("ok", rows_in=len(rows), rows_out=(n_links + len(waiting)) if a.commit else 0,
               counters={"submissions": len(rows), "pasted": n_links, "companies": len(pastes), "already_waiting": len(waiting), "orphans": len(orphans)},
               notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
