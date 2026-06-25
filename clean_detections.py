#!/usr/bin/env python3
"""
clean_detections.py — collapse the detections table to an actionable review set, WITHOUT
deleting anything. It sets the `status` field so you can filter to a clean view and still keep
the full history:

  status = "duplicate"  the redundant copies of a detection_id (idempotency was missing; one
                        canonical row per detection_id is kept as "new")
  status = "excluded"   detections sourced from a news page (page_type=news) or classified as a
                        press release (doc_type=press_release)
  status = "archive"    only with --archive-stale: detections whose `recent` is not checked
                        (older documents, not this/last year)
  status = "new"        what remains: the deduped, non-news, recent report detections to review

Which copy of a duplicate is kept: the one already classified (doc_type + label present) wins,
then the earliest `detected`, then a stable id order.

Run order: clean this table BEFORE deploying the upsert monitor, so detection_id is unique and
upsert can match exactly one row.

Modes:
  --source csv  --csv detections.csv      analyze a local export (no writes, for a dry preview)
  --source airtable                       read the live table (default); dry-run unless --commit
  --commit                                with airtable source, actually write the status values
  --archive-stale                         also archive non-recent detections

Env (airtable source): AIRTABLE_TOKEN, AIRTABLE_BASE, AIRTABLE_DETECTIONS_TABLE (default detections)
"""
import argparse, os, sys, time, collections
from urllib.parse import quote

F_ID="detection_id"; F_DETECTED="detected"; F_WBA="wba_id"; F_PTYPE="page_type"
F_DTYPE="doc_type"; F_RECENT="recent"; F_LABEL="label"; F_STATUS="status"
API = "https://api.airtable.com/v0"


def classified(fields):
    return bool(str(fields.get(F_DTYPE, "")).strip()) and bool(str(fields.get(F_LABEL, "")).strip())


def is_news_or_press(fields):
    return (str(fields.get(F_PTYPE, "")).strip().lower() == "news"
            or str(fields.get(F_DTYPE, "")).strip().lower() == "press_release")


def is_recent(fields):
    return str(fields.get(F_RECENT, "")).strip().lower() in ("checked", "true", "1", "yes")


def decide(records, archive_stale):
    """records: list of (key, fields). Returns {key: action} and counters.
    action in {keep_new, duplicate, excluded, archive}."""
    groups = collections.defaultdict(list)
    for key, f in records:
        groups[str(f.get(F_ID, "")).strip()].append((key, f))

    action = {}
    for did, members in groups.items():
        # choose the survivor of the detection_id group
        if did and len(members) > 1:
            survivor = sorted(
                members,
                key=lambda kf: (not classified(kf[1]),
                                str(kf[1].get(F_DETECTED, "")),
                                str(kf[0]))
            )[0][0]
        else:
            survivor = members[0][0]
        for key, f in members:
            if key != survivor and did:
                action[key] = "duplicate"
            elif is_news_or_press(f):
                action[key] = "excluded"
            elif archive_stale and not is_recent(f):
                action[key] = "archive"
            else:
                action[key] = "keep_new"
    return action


def report(records, action):
    c = collections.Counter(action.values())
    total = len(records)
    print("=" * 56)
    print(f"detections: {total}")
    print(f"  keep as 'new' (review set): {c['keep_new']}")
    print(f"  -> 'duplicate'            : {c['duplicate']}")
    print(f"  -> 'excluded' (news/press): {c['excluded']}")
    print(f"  -> 'archive'  (not recent): {c['archive']}")
    print("=" * 56)
    # concentration of the surviving review set
    keep = [f for (k, f) in records if action.get(k) == "keep_new"]
    by = collections.Counter(str(f.get(F_WBA, "")) for f in keep)
    if keep:
        top = by.most_common(10)
        share = sum(n for _, n in top) / len(keep) * 100
        print(f"review set: {len(keep)} rows across {len(by)} companies; "
              f"top 10 = {share:.0f}% of it")
        for wid, n in top:
            print(f"    {wid}: {n}")


def from_csv(path):
    import csv
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return [(i, r) for i, r in enumerate(rows)]


def from_airtable(base, token, table):
    import requests
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"pageSize": 100}; out = []; offset = None
    while True:
        if offset: params["offset"] = offset
        r = requests.get(url, headers=headers, params=params, timeout=30); r.raise_for_status()
        j = r.json()
        out.extend((rec["id"], rec.get("fields", {})) for rec in j.get("records", []))
        offset = j.get("offset"); time.sleep(0.22)
        if not offset: break
    return out


def commit_airtable(base, token, table, records, action):
    import requests
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    label = {"duplicate": "duplicate", "excluded": "excluded", "archive": "archive"}
    updates = []
    for k, f in records:
        a = action.get(k)
        if a in label:
            updates.append({"id": k, "fields": {F_STATUS: label[a]}})
    kept = sum(1 for a in action.values() if a == "keep_new")
    print(f"writing status on {len(updates)} rows (leaving {kept} as 'new') ...")
    for i in range(0, len(updates), 10):
        r = requests.patch(url, headers=headers,
                           json={"records": updates[i:i + 10], "typecast": True}, timeout=30)
        r.raise_for_status(); time.sleep(0.22)
        if i % 1000 == 0 and i:
            print(f"  {i}/{len(updates)}")
    print("done.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["airtable", "csv"], default="airtable")
    ap.add_argument("--csv")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--archive-stale", action="store_true")
    args = ap.parse_args()

    if args.source == "csv":
        if not args.csv:
            sys.exit("--csv PATH required with --source csv")
        records = from_csv(args.csv)
    else:
        token = os.environ.get("AIRTABLE_TOKEN"); base = os.environ.get("AIRTABLE_BASE")
        table = os.environ.get("AIRTABLE_DETECTIONS_TABLE", "detections")
        if not (token and base):
            sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
        records = from_airtable(base, token, table)

    action = decide(records, args.archive_stale)
    report(records, action)

    if args.source == "airtable" and args.commit:
        commit_airtable(os.environ["AIRTABLE_BASE"], os.environ["AIRTABLE_TOKEN"],
                        os.environ.get("AIRTABLE_DETECTIONS_TABLE", "detections"), records, action)
    elif args.source == "airtable":
        print("\nDRY-RUN. Re-run with --commit to write these status values.")


if __name__ == "__main__":
    main()
