#!/usr/bin/env python3
"""
archive_copy.py: the PDF of each document to be archived, copied to lanterN's own storage before anything is deleted.

Archiving a document deletes its row in Airtable, and the attached PDF with it. The owner's third condition
(9 Nov 2026): the file must be kept. For every candidate of archive_documents.py (same rule, same checks: periodic,
typed, text in the evidence database) that has no copy yet, this script:

  1. takes the PDF from the row's `file` attachment (the link is asked for again for each small batch: Airtable's
     attachment links expire after two hours);
  2. uploads it to the private bucket `archive` of the project's storage, at <wba_id>/<library_id>.pdf;
  3. asks the storage for the object and compares its size with the bytes sent;
  4. only then writes `stored_file` on the row: "archive/<wba_id>/<library_id>.pdf (1234567 bytes)".

archive_documents.py --commit ticks only rows with `stored_file`, and purge_discarded.py refuses to delete an
archived row without it. A file larger than the storage's limit, or one that does not upload, is counted and left
as it is: the document is simply not archived. Rerun at will: rows with `stored_file` are skipped.

    python archive_copy.py [--rule B] [--limit 500] [--minutes 300] [--commit]
    python archive_copy.py --rule all --disk /srv/lantern/files --workers 8 --commit

--rule all (14 Nov 2026, the owner: lanterN keeps its own copy of every document, byte for byte, in the project's
storage, and a working copy on its own machine) takes every document with a file from lanterN's nightly copy of
the library (public.library), not only the candidates for archiving. --disk DIR also writes each file to
DIR/<wba_id>/<library_id>.pdf on the machine it runs on; a row is done when it has `stored_file` and, with --disk,
its file is on the disk. A file the storage refuses as too large is still kept on the disk. --workers copies
several files at once.

Dry run counts the candidates without a copy and their size; it downloads and uploads nothing.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE, SUPABASE_DB_URL (the text check), SUPABASE_URL, SUPABASE_SERVICE_KEY (the
storage; never printed); AIRTABLE_QA_TOKEN for the run record.
"""
import argparse, os, sys, time
from datetime import datetime, timezone
from urllib.parse import quote

BUCKET = "archive"
BATCH = 10
MAX_BYTES = 400 * 1024 * 1024      # a "PDF" larger than this is not copied in memory here; it stays in Airtable


def object_path(wba, library_id):
    """<wba_id>/<library_id>.pdf, only letters, digits, dash, underscore and dot (anything else becomes _)."""
    import re
    clean = lambda t: re.sub(r"[^A-Za-z0-9._-]", "_", (t or "").strip()) or "unknown"
    return f"{clean(wba)}/{clean(library_id)}.pdf"


def stored_note(path, size):
    return f"{BUCKET}/{path} ({size} bytes)"


class Storage:
    """The few calls of the storage API this script needs. session: requests-like (get, post)."""
    def __init__(self, base_url, key, session):
        self.url = base_url.rstrip("/") + "/storage/v1"
        self.h = {"Authorization": f"Bearer {key}", "apikey": key}
        self.s = session

    def ensure_bucket(self):
        r = self.s.get(f"{self.url}/bucket/{BUCKET}", headers=self.h, timeout=60)
        if r.status_code == 200:
            return "there"
        r = self.s.post(f"{self.url}/bucket", headers=self.h, json={"id": BUCKET, "name": BUCKET, "public": False}, timeout=60)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"the bucket could not be made: HTTP {r.status_code} {r.text[:200]}")
        return "made"

    def upload(self, path, data):
        """'ok', 'too large' or raises."""
        r = self.s.post(f"{self.url}/object/{BUCKET}/{quote(path)}", headers=self.h | {"Content-Type": "application/pdf", "x-upsert": "true"},
                        data=data, timeout=600)
        if r.status_code in (200, 201):
            return "ok"
        if r.status_code == 413 or "exceeded the maximum allowed size" in r.text:
            return "too large"
        raise RuntimeError(f"upload HTTP {r.status_code} {r.text[:200]}")

    def size(self, path):
        """The stored object's size in bytes, None when it is not there."""
        folder, name = path.rsplit("/", 1)
        r = self.s.post(f"{self.url}/object/list/{BUCKET}", headers=self.h, json={"prefix": folder, "search": name, "limit": 10}, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"list HTTP {r.status_code} {r.text[:200]}")
        for o in r.json():
            if o.get("name") == name:
                return int((o.get("metadata") or {}).get("size") or 0)
        return None


def disk_path(disk, wba, library_id):
    return os.path.join(disk, object_path(wba, library_id))


def write_disk(disk, wba, library_id, data):
    """The file on this machine's disk, whole or not at all (written beside and renamed)."""
    path = disk_path(disk, wba, library_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return path


def copy_one(storage, session, wba, library_id, attachment, disk=None, store=True):
    """('stored', note) | ('on disk' | 'no file' | 'too large' | 'size differs' | 'failed: ...', None). attachment:
    the row's first `file` entry ({url, size}). disk: a directory that also gets the file (written before the
    upload, so a file the storage refuses is kept there). store=False: the storage already has it, only the disk
    copy is missing."""
    if not attachment or not attachment.get("url"):
        return "no file", None
    if int(attachment.get("size") or 0) > MAX_BYTES:
        return "too large", None
    r = session.get(attachment["url"], timeout=600)
    if r.status_code != 200 or not r.content:
        return f"failed: download HTTP {r.status_code}", None
    data, path = r.content, object_path(wba, library_id)
    if disk:
        try:
            write_disk(disk, wba, library_id, data)
        except Exception as e:
            return f"failed: disk {type(e).__name__}", None
    if not store:
        return "on disk", None
    try:
        if storage.upload(path, data) == "too large":
            return "too large", None
        got = storage.size(path)
    except Exception as e:
        return f"failed: {str(e)[:120]}", None
    if got != len(data):
        return "size differs", None
    return "stored", stored_note(path, len(data))


WAITS = (60, 300, 300, 600)          # seconds between further tries when Airtable stays down


def patient(call, waits=WAITS, sleep=time.sleep):
    """A call to Airtable that has already been retried for a minute (base_health.req) is tried again after each
    wait. On 11 Nov 2026 Airtable answered 503 for longer than a minute and a copy that had run for 228 minutes
    ended there, two hours before its time; the rest of the night was lost. Raises after the last wait."""
    for i, w in enumerate((0,) + tuple(waits)):
        if w:
            print(f"  Airtable did not answer; waiting {w} s (try {i + 1} of {len(waits) + 1})", flush=True)
            sleep(w)
        try:
            return call()
        except Exception as e:
            last = e
    raise last


def needs(fields, disk, exists=os.path.exists):
    """(store, on_disk) still to do for a row: the storage copy when `stored_file` is empty, the disk copy when a
    disk is given and the file is not there."""
    store = not (fields.get("stored_file") or "").strip()
    on_disk = bool(disk) and not exists(disk_path(disk, fields.get("wba_id"), fields.get("library_id")))
    return store, on_disk


def all_documents():
    """Every document with a file, from lanterN's nightly copy of the library: [(record id, fields)]."""
    import evidence_db
    with evidence_db.connect() as conn:
        cur = conn.cursor()
        cur.execute("select record_id, wba_id, library_id, nullif(fields->>'file_size_mb', ''), coalesce(fields->>'stored_file', '') "
                    "from public.library where gone is null and not discard and nullif(fields->>'file_size_mb', '') is not null "
                    "and record_id is not null")
        return [(rid, {"wba_id": w, "library_id": lid, "file_size_mb": mb, "stored_file": sf}) for rid, w, lid, mb, sf in cur.fetchall()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rule", choices=["A", "B", "all"], default="B")
    ap.add_argument("--disk", default="", help="also keep each file under this directory of this machine")
    ap.add_argument("--workers", type=int, default=1, help="files copied at the same time")
    ap.add_argument("--limit", type=int, default=500, help="documents this run (0 = all)")
    ap.add_argument("--minutes", type=float, default=300, help="stop starting new batches after this")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    for v in ("AIRTABLE_TOKEN", "AIRTABLE_BASE"):
        if not os.environ.get(v):
            sys.exit(f"Set {v}.")
    import collections, requests, qa
    from archive_documents import select_candidates, size_gb
    from base_health import req, API, BASE
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
        for v in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY"):
            if not os.environ.get(v):
                sys.exit(f"Set {v} (the storage the copies go to).")
    run = qa.Run("archive_copy.py")
    t0 = time.time()
    if a.rule == "all":
        cands = all_documents()
    else:
        cands, dropped = select_candidates(a.rule, datetime.now(timezone.utc).date())
    todo = [(rid, f) for rid, f in cands if any(needs(f, a.disk))]
    todo.sort(key=lambda x: (x[1].get("wba_id") or "", x[1].get("library_id") or ""))
    c = collections.Counter({"candidates": len(cands), "already stored": len(cands) - len(todo), "to copy": len(todo)})
    print(f"{len(cands):,} candidates of rule {a.rule}: {c['already stored']:,} already stored, {len(todo):,} to copy ({size_gb(todo):,.1f} GB)"
          + (f"; disk copy under {a.disk}" if a.disk else ""), flush=True)
    if a.limit:
        todo = todo[:a.limit]
    sent = 0
    if a.commit and todo:
        session = requests.Session()
        storage = Storage(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"].strip(), session)
        print(f"  bucket {BUCKET}: {storage.ensure_bucket()}", flush=True)
        url = f"{API}/{BASE}/{quote('report_library')}"
        step = BATCH if a.workers <= 1 else max(BATCH, min(50, a.workers * 5))
        for i in range(0, len(todo), step):
            if time.time() - t0 > a.minutes * 60:
                c["stopped at the time cap"] = 1
                break
            batch = todo[i:i + step]
            formula = "OR(" + ",".join(f"RECORD_ID()='{rid}'" for rid, _ in batch) + ")"
            fresh = patient(lambda: req("GET", url, params=[("filterByFormula", formula), ("fields[]", "file"), ("fields[]", "stored_file"), ("pageSize", "100")])).json().get("records", [])
            files = {r["id"]: r.get("fields", {}) for r in fresh}
            updates = []

            def work(item):
                rid, f = item
                now = files.get(rid, {})
                store, on_disk = needs(dict(f, stored_file=now.get("stored_file") or ""), a.disk)
                if not store and not on_disk:
                    return rid, f, "already stored", None
                try:
                    return (rid, f) + copy_one(storage, requests.Session() if a.workers > 1 else session, f.get("wba_id"), f.get("library_id"),
                                               (now.get("file") or [None])[0], disk=a.disk or None, store=store)
                except Exception as e:                      # one file must not end the run
                    return rid, f, f"failed: {type(e).__name__}", None

            if a.workers > 1:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=a.workers) as ex:
                    results = list(ex.map(work, batch))
            else:
                results = [work(x) for x in batch]
            for rid, f, what, note in results:
                if what == "already stored":
                    c["already stored"] += 1; continue
                c[what if not what.startswith("failed") else "failed"] += 1
                if what.startswith("failed") and c["failed"] <= 5:
                    print(f"  {f.get('library_id')}: {what}", flush=True)
                if note:
                    updates.append({"id": rid, "fields": {"stored_file": note}})
                    sent += int(note.rsplit("(", 1)[1].split()[0])
            for k in range(0, len(updates), 10):               # Airtable takes ten rows a call
                part = updates[k:k + 10]
                patient(lambda: req("PATCH", url, json={"records": part}))
            if (i // step) % 20 == 19:
                print(f"  {c['stored']:,} stored, {c['on disk']:,} on disk only, {sent / 1e9:.1f} GB to the storage, {(time.time() - t0) / 60:.0f} min", flush=True)
    md = [f"### Archive copy, rule {a.rule}" + ("" if a.commit else " (dry run: nothing downloaded or uploaded)"), "",
          "| | Count |", "| --- | --: |"] + [f"| {k} | {v:,} |" for k, v in sorted(c.items())] + ["", f"Bytes stored this run: {sent / 1e9:.2f} GB."]
    out = "\n".join(md) + "\n"
    open("archive_copy.md", "w").write(out)
    print(out, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(out)
    run.finish("ok", rows_in=len(todo), rows_out=c["stored"], counters=dict(c), notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
