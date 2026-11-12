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


def copy_one(storage, session, wba, library_id, attachment):
    """('stored', note) | ('no file' | 'too large' | 'size differs' | 'failed: ...', None). attachment: the row's
    first `file` entry ({url, size})."""
    if not attachment or not attachment.get("url"):
        return "no file", None
    if int(attachment.get("size") or 0) > MAX_BYTES:
        return "too large", None
    r = session.get(attachment["url"], timeout=600)
    if r.status_code != 200 or not r.content:
        return f"failed: download HTTP {r.status_code}", None
    data, path = r.content, object_path(wba, library_id)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rule", choices=["A", "B"], default="B")
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
    cands, dropped = select_candidates(a.rule, datetime.now(timezone.utc).date())
    todo = [(rid, f) for rid, f in cands if not (f.get("stored_file") or "").strip()]
    todo.sort(key=lambda x: (x[1].get("wba_id") or "", x[1].get("library_id") or ""))
    c = collections.Counter({"candidates": len(cands), "already stored": len(cands) - len(todo), "to copy": len(todo)})
    print(f"{len(cands):,} candidates of rule {a.rule}: {c['already stored']:,} already stored, {len(todo):,} to copy ({size_gb(todo):,.1f} GB)", flush=True)
    if a.limit:
        todo = todo[:a.limit]
    sent = 0
    if a.commit and todo:
        session = requests.Session()
        storage = Storage(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"].strip(), session)
        print(f"  bucket {BUCKET}: {storage.ensure_bucket()}", flush=True)
        url = f"{API}/{BASE}/{quote('report_library')}"
        for i in range(0, len(todo), BATCH):
            if time.time() - t0 > a.minutes * 60:
                c["stopped at the time cap"] = 1
                break
            batch = todo[i:i + BATCH]
            formula = "OR(" + ",".join(f"RECORD_ID()='{rid}'" for rid, _ in batch) + ")"
            fresh = patient(lambda: req("GET", url, params=[("filterByFormula", formula), ("fields[]", "file"), ("fields[]", "stored_file"), ("pageSize", "100")])).json().get("records", [])
            files = {r["id"]: r.get("fields", {}) for r in fresh}
            updates = []
            for rid, f in batch:
                now = files.get(rid, {})
                if (now.get("stored_file") or "").strip():
                    c["already stored"] += 1; continue
                what, note = copy_one(storage, session, f.get("wba_id"), f.get("library_id"), (now.get("file") or [None])[0])
                c[what if not what.startswith("failed") else "failed"] += 1
                if what.startswith("failed") and c["failed"] <= 5:
                    print(f"  {f.get('library_id')}: {what}", flush=True)
                if note:
                    updates.append({"id": rid, "fields": {"stored_file": note}})
                    sent += int(note.rsplit("(", 1)[1].split()[0])
            if updates:
                patient(lambda: req("PATCH", url, json={"records": updates}))
            if (i // BATCH) % 20 == 19:
                print(f"  {c['stored']:,} stored, {sent / 1e9:.1f} GB, {(time.time() - t0) / 60:.0f} min", flush=True)
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
