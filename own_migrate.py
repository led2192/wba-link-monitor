#!/usr/bin/env python3
"""
own_migrate.py: the companies that are not SDG2000 and were put in Airtable leave it for lantrNa's own lane.

Between 11 and 26 Nov 2026 a company added by a person or from the catalogue (ids LN_) got a row of `companies`, and
the robot kept its pages (`monitored_links`) and documents (`report_library`) in the Disclosure Library. The owner's
rule of 26 Nov: only SDG2000 companies (ids PT_) live there. This script moves the others, in three steps that are
run one at a time and can each be run again:

  (no flag)   counts, and writes nothing: the companies, their pages and their documents in Airtable (live,
              discarded, with a file, with the file's copy in lantrNa's storage), and their documents in the evidence
              database.
  --stop      only the pages are no longer monitored, so that nothing new of these companies arrives while their
              files are being copied (archive_copy.py copies the files of documents that are not discarded, so the
              copy comes before --move). Reversible.
  --move      the evidence database learns them as the lane's own (own_lane.py): own_companies; own_pages from the
              pages still monitored; own_docs from the documents the database holds (with what was read of each) and
              from the library's rows it does not hold yet (to be fetched by the lane). A document with a file and
              no copy of it gets its copy first (archive_copy.copy_one: the storage and, with --disk, the server's
              disk), because once discarded no other job would copy it. From then on load_supabase.py
              leaves these documents as they are. Then, in Airtable and reversible: the pages are no longer monitored,
              the documents are discarded with the reason MOVED, the company's intake_note says where it went.
  --delete    after the owner has seen the counts: every row of the three tables for these companies is written to
              an archive (all fields, one JSON line a row, gzipped) in the storage's private bucket `archive`, under
              _moved/, and only then deleted. A document with a file and no copy of it (stored_file empty) is NOT
              deleted: its row would take the only copy with it. Not reversible, except by hand from the archive.

    python own_migrate.py [--stop | --move | --delete] [--commit]

Without --commit --move and --delete say what they would do. Prints ids and counts only. Env: AIRTABLE_TOKEN,
AIRTABLE_BASE, SUPABASE_DB_URL; SUPABASE_URL and SUPABASE_SERVICE_KEY for the archive of --delete.
"""
import argparse, collections, gzip, io, json, os, sys, time
from datetime import date
from urllib.parse import quote

MOVED = "Moved out of Airtable: not SDG2000"
API = "https://api.airtable.com/v0"
TABLES = ("report_library", "monitored_links", "companies")


def is_sdg(wba):
    """An SDG2000 company: its id begins PT_. Everything else is not (the owner's rule)."""
    return str(wba or "").strip().startswith("PT_")


def inventory(companies, pages, docs):
    """Counts of what Airtable holds for the companies that are not SDG2000."""
    c = collections.Counter()
    c["companies"] = len(companies); c["pages"] = len(pages); c["pages monitored"] = sum(1 for r in pages if r["fields"].get("monitor"))
    c["documents"] = len(docs)
    for r in docs:
        f = r["fields"]
        c["documents discarded"] += 1 if f.get("discard") else 0
        c["documents with a file"] += 1 if f.get("file") else 0
        c["documents with a file and no copy"] += 1 if f.get("file") and not (f.get("stored_file") or "").strip() else 0
    return c


def airtable_patches(companies, pages, docs, today):
    """What --move changes in Airtable: {table: [(record id, fields)]}. Pages still monitored are switched off,
    documents not yet discarded are discarded with the reason, a company's note is extended once."""
    note = f"moved to lantrNa's own lane ({today})"
    return {"monitored_links": [(r["id"], {"monitor": False}) for r in pages if r["fields"].get("monitor")],
            "report_library": [(r["id"], {"discard": True, "discard_reason": MOVED}) for r in docs if not r["fields"].get("discard")],
            "companies": [(r["id"], {"intake_note": ((r["fields"].get("intake_note") or "").strip() + "; " + note).lstrip("; ")})
                          for r in companies if "moved to lantrNa's own lane" not in (r["fields"].get("intake_note") or "")]}


def deletable(docs):
    """(rows that may be deleted, rows kept): a document with a file and no copy of it is kept."""
    keep = [r for r in docs if r["fields"].get("file") and not (r["fields"].get("stored_file") or "").strip()]
    kept = {r["id"] for r in keep}
    return [r for r in docs if r["id"] not in kept], keep


def archive_bytes(rows_by_table):
    """Every row with every field, one JSON object a line, gzipped."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as fh:
        for table, rows in rows_by_table.items():
            for r in rows:
                fh.write((json.dumps({"table": table, "id": r["id"], "createdTime": r.get("createdTime"), "fields": r.get("fields", {})}, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    return buf.getvalue()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--disk", default="")
    ap.add_argument("--stop", action="store_true"); ap.add_argument("--move", action="store_true"); ap.add_argument("--delete", action="store_true"); ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    for v in ("AIRTABLE_TOKEN", "AIRTABLE_BASE", "SUPABASE_DB_URL"):
        if not os.environ.get(v):
            sys.exit(f"Set {v}.")
    if a.stop + a.move + a.delete > 1:
        sys.exit("One step at a time: --move, and --delete on another run.")
    import requests, qa, evidence_db
    from monitor_core import airtable_request
    from ids import link_id
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("own_migrate.py")
    base, H = os.environ["AIRTABLE_BASE"], {"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}", "Content-Type": "application/json"}

    def sweep(table, fields=None):
        out, offset = [], None
        while True:
            p = [("pageSize", "100"), ("filterByFormula", "AND({wba_id}!='', LEFT({wba_id}&'', 3)!='PT_')")] + [("fields[]", f) for f in (fields or [])] + ([("offset", offset)] if offset else [])
            j = airtable_request("GET", f"{API}/{base}/{quote(table)}", H, params=p).json()
            out += [r for r in j.get("records", []) if not is_sdg(r.get("fields", {}).get("wba_id"))]; offset = j.get("offset")
            if not offset:
                return out
            time.sleep(0.2)

    whole = a.delete            # the archive needs every field; the other steps only the ones they read
    companies = sweep("companies", None if whole else ["wba_id", "intake_note", "website"])
    pages = sweep("monitored_links", None if whole else ["wba_id", "url", "type", "monitor"])
    docs = sweep("report_library", None if whole else ["wba_id", "library_id", "document_url", "discard", "file", "stored_file"])
    c = inventory(companies, pages, docs)
    wbas = sorted({r["fields"]["wba_id"].strip() for r in companies} | {r["fields"]["wba_id"].strip() for r in docs} | {r["fields"]["wba_id"].strip() for r in pages})
    with evidence_db.connect() as conn:
        cur = conn.cursor()
        cur.execute("select count(*), count(*) filter (where coalesce(pages, 0) > 0) from public.documents where wba_id = any(%s)", (wbas,))
        c["documents in the evidence database"], c["of them with text"] = cur.fetchone()
        cur.execute("select count(*) from public.own_companies where wba_id = any(%s)", (wbas,)); c["companies already in the lane"] = cur.fetchone()[0]
        print("In Airtable, not SDG2000: " + "; ".join(f"{k} {v:,}" for k, v in c.items()) + ".", flush=True)

        if a.stop:
            rows = airtable_patches(companies, pages, docs, date.today().isoformat())["monitored_links"]
            print(f"--stop: monitored_links {len(rows):,} pages to switch off" + ("" if a.commit else " (not written)"), flush=True)
            if a.commit:
                for i in range(0, len(rows), 10):
                    airtable_request("PATCH", f"{API}/{base}/{quote('monitored_links')}", H, payload={"records": [{"id": rid, "fields": f} for rid, f in rows[i:i + 10]]})
                    time.sleep(0.22)
                print(f"  monitored_links: {len(rows):,} rows changed", flush=True)

        if a.move:
            patches = airtable_patches(companies, pages, docs, date.today().isoformat())
            print("--move: " + ", ".join(f"{t} {len(p):,} rows to change" for t, p in patches.items()) + ("" if a.commit else " (not written)"), flush=True)
            if a.commit:
                missing = [r for r in docs if r["fields"].get("file") and not (r["fields"].get("stored_file") or "").strip() and (r["fields"].get("library_id") or "").strip()]
                if missing and os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"):
                    from archive_copy import Storage, copy_one
                    sess = requests.Session(); st = Storage(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"].strip(), sess); got = collections.Counter(); notes = []
                    for r in missing:
                        f = r["fields"]
                        try:
                            what, note = copy_one(st, sess, f["wba_id"].strip(), f["library_id"].strip(), (f.get("file") or [None])[0], disk=a.disk or None)
                        except Exception as e:
                            what, note = f"failed: {type(e).__name__}", None
                        got[what.split(":")[0]] += 1
                        if note:
                            f["stored_file"] = note; notes.append((r["id"], {"stored_file": note}))
                    for i in range(0, len(notes), 10):
                        airtable_request("PATCH", f"{API}/{base}/{quote('report_library')}", H, payload={"records": [{"id": rid, "fields": f} for rid, f in notes[i:i + 10]]})
                        time.sleep(0.22)
                    print("  files with no copy: " + ", ".join(f"{k} {v:,}" for k, v in got.most_common()), flush=True)
                cur.execute("insert into public.own_companies (wba_id, note) select w, 'moved from Airtable' from unnest(%s::text[]) w where exists (select 1 from public.companies c where c.wba_id = w) on conflict do nothing", (wbas,))
                cur.executemany("insert into public.own_pages (id, wba_id, url, kind, how) values (%s, %s, %s, %s, 'moved') on conflict (id) do nothing",
                                [(link_id(r["fields"]["wba_id"].strip(), r["fields"].get("url") or ""), r["fields"]["wba_id"].strip(), r["fields"].get("url"), str(r["fields"].get("type") or "")[:60] or None)
                                 for r in pages if r["fields"].get("monitor") and (r["fields"].get("url") or "").startswith("http")])
                cur.execute("""insert into public.own_docs (library_id, wba_id, url, how, status, tries, read_at)
                               select d.library_id, d.wba_id, d.url, 'moved', coalesce(d.text_status, 'new'), 0, case when coalesce(d.pages, 0) > 0 then now() end
                                 from public.documents d where d.wba_id = any(%s) on conflict (library_id) do nothing""", (wbas,))
                stored = [((r["fields"].get("stored_file") or "").strip() or None, (r["fields"].get("library_id") or "").strip()) for r in docs]
                cur.executemany("update public.own_docs set stored = coalesce(stored, %s) where library_id = %s", [s for s in stored if s[0] and s[1]])
                cur.executemany("insert into public.own_docs (library_id, wba_id, url, how) values (%s, %s, %s, 'moved') on conflict (library_id) do nothing",
                                [((r["fields"].get("library_id") or "").strip(), r["fields"]["wba_id"].strip(), r["fields"].get("document_url"))
                                 for r in docs if (r["fields"].get("library_id") or "").strip() and not r["fields"].get("discard") and str(r["fields"].get("document_url") or "").lower().split("?")[0].endswith(".pdf")])
                cur.execute("update public.company_requests set status = 'added', result = 'followed by lantrNa itself, outside Airtable' where wba_id = any(%s) and status in ('new', 'waiting')", (wbas,))
                conn.commit()
                for table in TABLES:          # the documents first: if the reason is not accepted, nothing else has changed in Airtable
                    rows = patches[table]
                    for i in range(0, len(rows), 10):
                        airtable_request("PATCH", f"{API}/{base}/{quote(table)}", H, payload={"records": [{"id": rid, "fields": f} for rid, f in rows[i:i + 10]]})
                        time.sleep(0.22)
                    print(f"  {table}: {len(rows):,} rows changed", flush=True)

        if a.delete:
            may, keep = deletable(docs)
            plan = {"report_library": may, "monitored_links": pages, "companies": companies if not keep else [r for r in companies if r["fields"]["wba_id"].strip() not in {k["fields"]["wba_id"].strip() for k in keep}]}
            not_moved = [r for r in may if not r["fields"].get("discard")]
            print("--delete: " + ", ".join(f"{t} {len(r):,} rows" for t, r in plan.items()) + f"; {len(keep):,} documents kept because their file has no copy" + ("" if a.commit else " (not deleted)"), flush=True)
            if not_moved or c["companies already in the lane"] < len({r['fields']['wba_id'].strip() for r in companies}):
                print("Not deleted: run --move --commit first (some documents are not discarded or some companies are not in the lane).", flush=True)
            elif a.commit:
                if not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY")):
                    sys.exit("Set SUPABASE_URL and SUPABASE_SERVICE_KEY: nothing is deleted without its archive.")
                from archive_copy import Storage
                st = Storage(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"].strip(), requests.Session())
                data = archive_bytes(plan); path = f"_moved/not-sdg2000-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}.jsonl.gz"
                if st.upload(path, data) != "ok" or (st.size(path) or 0) != len(data):
                    sys.exit("The archive could not be stored whole: nothing deleted.")
                print(f"  archive stored: archive/{path} ({len(data):,} bytes)", flush=True)
                for table in TABLES:
                    ids = [r["id"] for r in plan[table]]
                    for i in range(0, len(ids), 10):
                        airtable_request("DELETE", f"{API}/{base}/{quote(table)}", H, params=[("records[]", x) for x in ids[i:i + 10]])
                        time.sleep(0.22)
                    print(f"  {table}: {len(ids):,} rows deleted", flush=True)
    run.finish("ok", rows_in=c["documents"], rows_out=0, counters=dict(c), notes="move" if a.move else "delete" if a.delete else "count")
    return 0


if __name__ == "__main__":
    sys.exit(main())
