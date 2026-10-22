#!/usr/bin/env python3
"""
qa_archive.py — keeps the Pipeline QA base small without losing history.

Closed failures (status fixed, resolved_silently or wontfix) whose last_seen is more than --days ago
(default 90) are written, every field, to a gzipped JSONL file uploaded to the private text-store
repository (TEXT_STORE_REPO), release "qa-archive", as qa_failures_archive_<date>.jsonl.gz. Only once
the upload is confirmed (the asset exists, complete, with the right size) are those rows deleted from
the base. Open and expected failures, labels, evals and runs are never archived or deleted: labels and
evals are the system's memory and grow slowly. A failure that recurs after being archived simply opens
a new row (its history is in the archive under the same failure_key).

The archive lives in the private repository, not in this public one: failure rows name companies,
pages and documents.

Modes: dry run by default (prints what would be archived); --commit uploads, verifies, deletes.
Env: AIRTABLE_QA_TOKEN, AIRTABLE_QA_BASE, TEXT_STORE_REPO, TEXT_STORE_TOKEN.
"""
import argparse, datetime as dt, gzip, json, os, sys, time

import qa
import text_core as tc

CLOSED = ("fixed", "resolved_silently", "wontfix")
TAG = "qa-archive"


def formula(cutoff):
    st = "OR(" + ",".join(f"{{status}}='{s}'" for s in CLOSED) + ")"
    return f"AND({st}, IS_BEFORE({{last_seen}}, '{cutoff}'))"


def write_archive(rows, path):
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    return os.path.getsize(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()
    qtok = os.environ.get("AIRTABLE_QA_TOKEN")
    if not qtok:
        sys.exit("Set AIRTABLE_QA_TOKEN.")
    if args.commit:
        os.environ["QA_COMMIT"] = "true"
    today = dt.date.today()
    cutoff = (today - dt.timedelta(days=args.days)).isoformat()
    run = qa.Run("qa_archive.py", token=qtok)

    url = run._url(qa.T_FAILURES)
    params = [("pageSize", "100"), ("filterByFormula", formula(cutoff))]
    rows, offset = [], None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = qa._request("GET", url, run._headers(), params=p).json()
        rows += [{"id": r["id"], "createdTime": r.get("createdTime"), "fields": r.get("fields", {})}
                 for r in j.get("records", [])]
        offset = j.get("offset")
        if not offset:
            break
        time.sleep(0.12)
    print(f">>> {len(rows):,} closed failures last seen before {cutoff}", flush=True)
    counters = {"to_archive": len(rows), "cutoff": cutoff}
    if not rows:
        run.finish("ok", rows_in=0, rows_out=0, counters=counters)
        return
    name = f"qa_failures_archive_{today.isoformat()}.jsonl.gz"
    size = write_archive(rows, name)
    print(f">>> wrote {name} ({size:,} bytes)", flush=True)
    if not args.commit:
        print(">>> dry run: nothing uploaded, nothing deleted.", flush=True)
        run.finish("ok", rows_in=len(rows), rows_out=0, counters=counters)
        return

    store = tc.open_store()
    if not isinstance(store, tc.ReleaseStore):
        sys.exit("The archive needs the private repository store (TEXT_STORE_REPO + TEXT_STORE_TOKEN).")
    store.STATE_TAG = TAG                       # a release of its own, never pruned by the text jobs
    store.upload(name)
    rel = store._release(TAG)
    held = {a["name"]: a for a in store._paged(f"{store.API}/repos/{store.repo}/releases/{rel['id']}/assets")}
    a = held.get(name)
    if not (a and a.get("state") == "uploaded" and a.get("size") == size):
        run.failure("RUN_FAILED", "write_failed", key=f"qa_archive|{today}", stage="run",
                    message=f"archive {name} not confirmed in {store.repo}; nothing deleted")
        run.finish("failed", rows_in=len(rows), rows_out=0, counters=counters)
        sys.exit(f"Upload of {name} not confirmed; nothing deleted.")
    print(f">>> uploaded and verified in {store.repo} release {TAG}; deleting {len(rows):,} rows ...", flush=True)
    deleted = 0
    for i in range(0, len(rows), 10):
        ids = [r["id"] for r in rows[i:i + 10]]
        qa._request("DELETE", url, run._headers(), params=[("records[]", x) for x in ids])
        deleted += len(ids)
        time.sleep(0.2)
    counters.update({"archived": len(rows), "deleted": deleted, "file": name, "bytes": size})
    run.finish("ok", rows_in=len(rows), rows_out=deleted, counters=counters)
    print(f">>> archived {len(rows):,} failures to {name}; deleted {deleted:,} from the base.", flush=True)


if __name__ == "__main__":
    main()
