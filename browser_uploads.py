#!/usr/bin/env python3
"""
browser_uploads.py: the documents sent from a person's browser ("Send to lanterN") become rows of the library.

Some company sites refuse our robot from every network we have. The browser extension (extension/) downloads the
PDFs with the person's own browser and stores each in the private storage bucket `uploads` of the evidence
database; public.browser_uploads lists them (supabase/search.sql). This script takes the ones marked `stored` and
does what the inbox does for a file dropped by hand (inbox.py, add_file):

  - a row is recognised by its document's address (ids.doc_id), so a document we already knew by its link and
    could not download (file_status blocked) gets its file on the SAME row, and nothing is duplicated;
  - a document with no row yet gets one, with its address in document_url;
  - a file whose row already holds a file is marked `known` and nothing is written;
  - a file with no address (sent from a PDF that was open from the disk) gets the id of its name and size, as a
    hand upload does.

Airtable is given a signed link to the stored object (valid six hours) and fetches the file itself. Type, year,
pages and text follow from the usual jobs, as for any new row. Before that, each object is checked: it is there,
it has the registered size and it starts as a PDF does.

An upload whose company is not in the companies table is marked orphan and recorded as a failure: no row is
invented for it (orphans are logged, never fabricated).

    python browser_uploads.py [--commit] [--limit 200]

Dry run prints what it would do, by company, never who sent it. Env: SUPABASE_DB_URL, SUPABASE_URL,
SUPABASE_SERVICE_KEY, AIRTABLE_TOKEN, AIRTABLE_BASE; AIRTABLE_QA_TOKEN for the run record.
"""
import argparse, collections, hashlib, os, sys, time
from datetime import datetime, timezone
from urllib.parse import quote

BUCKET = "uploads"
SIGN_SECONDS = 6 * 3600


def library_id(wba, source_url, filename, size):
    """The row a sent file belongs to: the document's address when it has one (the id the harvester and the inbox
    give that address), else its name and size (the id the inbox gives a file dropped by hand)."""
    from ids import normalize
    src = (source_url or "").strip()
    key = (normalize(src) or src.lower()) if src else f"upload:{wba}:{(filename or '').lower()}:{size}"
    return f"{wba}-{hashlib.sha1(key.encode()).hexdigest()[:10]}"


def note_for(filename, source_url):
    return f"Sent from a person's browser ({filename})" + (f", downloaded from {source_url}." if source_url else ".")


def plan(uploads, companies, rows):
    """What to do with each upload. uploads: [dict(id, wba_id, source_url, filename, bytes)]; companies:
    {wba_id: name}; rows: {wba_id: [(record_id, library_id, document_url, has_file, discarded)]}, the company's rows.
    Returns [(upload, action, library_id, record_id)], action in fill | create | known | orphan.
    A row is recognised by its document, not by its id (ids.doc_id: the address without the parts that change on
    every visit), as the inbox does: the rows the harvester made carry another kind of id. A discarded row is not
    filled; a document only a discarded row holds is not made again under the same id. Two uploads of one document
    in a batch (two language pages linking the same file) give one row: the second is known."""
    from ids import doc_id
    out, taken = [], set()
    index = {}
    for wba, lst in rows.items():
        live, ids = {}, set()
        for rec, lid, url, has_file, discarded in lst:
            ids.add(lid)
            if url and not discarded:
                k = doc_id(wba, url)
                if k not in live or (has_file and not live[k][1]):
                    live[k] = (rec, has_file, lid)
        index[wba] = (live, ids)
    for u in uploads:
        wba = u["wba_id"]
        if wba not in companies:
            out.append((u, "orphan", None, None)); continue
        live, ids = index.get(wba, ({}, set()))
        src = (u.get("source_url") or "").strip()
        lid = library_id(wba, src, u.get("filename"), u.get("bytes"))
        rec, has_file, row_lid = live.get(doc_id(wba, src), (None, False, None)) if src else (None, False, None)
        key = row_lid or lid
        if has_file or key in taken or (not rec and lid in ids):
            out.append((u, "known", key, rec))
        elif rec:
            out.append((u, "fill", key, rec)); taken.add(key)
        else:
            out.append((u, "create", lid, None)); taken.add(lid)
    return out


class Storage:
    """The calls of the storage API this script needs. session: requests-like (get, post)."""
    def __init__(self, base_url, key, session):
        self.base = base_url.rstrip("/") + "/storage/v1"
        self.h = {"Authorization": f"Bearer {key}", "apikey": key}
        self.s = session

    def check(self, path, size):
        """'ok', or why not: the object is there, has the registered size and starts as a PDF."""
        r = self.s.get(f"{self.base}/object/{BUCKET}/{quote(path)}", headers=self.h | {"Range": "bytes=0-1023"}, timeout=120)
        if r.status_code not in (200, 206):
            return f"the file is not in the storage (HTTP {r.status_code})"
        total = (r.headers.get("Content-Range") or "").rsplit("/", 1)[-1] if r.status_code == 206 else str(len(r.content))
        if total.isdigit() and int(total) != int(size):
            return f"the stored file has {total} bytes, {size} were registered"
        if b"%PDF-" not in r.content[:1024]:
            return "the file is not a PDF"
        return "ok"

    def signed(self, path):
        r = self.s.post(f"{self.base}/object/sign/{BUCKET}/{quote(path)}", headers=self.h, json={"expiresIn": SIGN_SECONDS}, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"sign HTTP {r.status_code} {r.text[:200]}")
        j = r.json()
        return self.base + (j.get("signedURL") or j.get("signedUrl"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=200, help="uploads this run")
    a = ap.parse_args()
    need = ["SUPABASE_DB_URL", "AIRTABLE_TOKEN", "AIRTABLE_BASE"] + (["SUPABASE_URL", "SUPABASE_SERVICE_KEY"] if a.commit else [])
    for v in need:
        if not os.environ.get(v):
            sys.exit(f"Set {v}.")
    import requests, qa, evidence_db
    import harvest_reports as HR
    from base_health import sweep, API
    from monitor_core import airtable_request
    BASE = os.environ["AIRTABLE_BASE"]
    headers = {"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}", "Content-Type": "application/json"}
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("browser_uploads.py")
    c = collections.Counter()
    with evidence_db.connect() as conn:
        cur = conn.cursor()
        cur.execute("select to_regclass('public.browser_uploads') is not null")
        if not cur.fetchone()[0]:
            print("public.browser_uploads is not there yet: nothing to do.")
            run.finish("ok", rows_in=0, rows_out=0, notes="no table yet")
            return 0
        cur.execute("select id, wba_id, source_url, filename, bytes, path from public.browser_uploads where status = 'stored' order by id limit %s", (a.limit,))
        uploads = [dict(zip(("id", "wba_id", "source_url", "filename", "bytes", "path"), r)) for r in cur.fetchall()]
        if not uploads:
            print("No new file sent from a browser.")
            run.finish("ok", rows_in=0, rows_out=0, notes="nothing new")
            return 0
        wanted = {u["wba_id"] for u in uploads}
        companies = {}
        for rec in sweep("companies", ["wba_id", "company_name"]):
            f = rec.get("fields", {}); w = (f.get("wba_id") or "").strip()
            if w in wanted:
                companies[w] = f.get("company_name") or ""
        rows = {}
        for wba in sorted(companies):
            lst = rows.setdefault(wba, [])
            for rec in sweep("report_library", ["library_id", "document_url", "file", "discard"], "{wba_id}='%s'" % wba.replace("'", "")):
                f = rec.get("fields", {})
                lst.append((rec["id"], f.get("library_id"), f.get("document_url") or "", bool(f.get("file")), bool(f.get("discard"))))
        todo = plan(uploads, companies, rows)
        by = collections.Counter((u["wba_id"], act) for u, act, _, _ in todo)
        print(f"{len(uploads)} files sent from a browser:", flush=True)
        for (wba, act), n in sorted(by.items()):
            print(f"  {wba}: {n} {act}", flush=True)
        storage = Storage(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"].strip(), requests.Session()) if a.commit else None
        today = datetime.now(timezone.utc).date().isoformat()
        url = f"{API}/{BASE}/{quote('report_library')}"

        def mark(u, status, result):
            cur.execute("update public.browser_uploads set status = %s, result = %s where id = %s and status = 'stored'", (status, result, u["id"]))
            conn.commit()

        for u, act, lid, rec in todo:
            c[act] += 1
            if act == "orphan":
                run.failure("DATA_CHECK", f"a file was sent for {u['wba_id']}, which is not in the companies table", key=f"browser_upload|{u['id']}")
            if not a.commit:
                continue
            if act == "orphan":
                mark(u, "orphan", "the company is not in the companies table"); continue
            if act == "known":
                mark(u, "known", f"the library already holds this document ({lid})"); continue
            why = storage.check(u["path"], u["bytes"])
            if why != "ok":
                c["failed"] += 1; c[act] -= 1
                run.failure("DATA_CHECK", f"a file sent from a browser was not taken: {why}", key=f"browser_upload|{u['id']}")
                mark(u, "failed", why); continue
            fname = u["filename"] or "document.pdf"
            if not fname.lower().endswith(".pdf"):
                fname += ".pdf"
            file_cell = [{"url": storage.signed(u["path"]), "filename": fname}]
            note = note_for(fname, u["source_url"])
            if act == "fill":
                airtable_request("PATCH", f"{url}/{rec}", headers, payload={"fields": {"file": file_cell, "Reviewer notes": note}})
            else:
                fields = {"library_id": lid, "wba_id": u["wba_id"], "company_name": companies[u["wba_id"]], "file": file_cell,
                          "doc_year": HR.year_of(u["source_url"] or fname) or HR.year_of(fname), "first_seen": today, "Reviewer notes": note}
                if u["source_url"]:
                    fields["document_url"] = u["source_url"]
                airtable_request("POST", url, headers, payload={"fields": {k: v for k, v in fields.items() if v not in (None, "")}, "typecast": True})   # no link field among them
            mark(u, "taken", f"{'file put on the existing row' if act == 'fill' else 'new row'} {lid}")
            time.sleep(0.25)
    print(f"{c['create']} new rows, {c['fill']} files put on rows that had none, {c['known']} already held, {c['failed']} failed, {c['orphan']} without a company"
          + ("" if a.commit else " (dry run: nothing written)"), flush=True)
    run.finish("ok", rows_in=len(uploads), rows_out=(c["create"] + c["fill"]) if a.commit else 0, counters=dict(c), notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
