#!/usr/bin/env python3
"""
lantern_ids.py: lanterN's own permanent id for every company, written beside the WBA id.

Why (12 Nov 2026, phase 1 of the plan to run lanterN on its own): every script joins on `wba_id`, WBA's id
(PT_ and a number). Companies added since 11 Nov already carry lanterN's own id there (LN_ and 8 hex digits,
`add_companies.company_id`). For lanterN to stand without WBA's list, every company needs an id of its own,
the 2,000 of the SDG2000 included. This script writes it to `lantern_id` in the companies table. Nothing reads
it yet and `wba_id` stays the key everywhere: this step only makes sure both ids exist, with a lookup either
way (Airtable holds both on the row; `load_companies.py` copies both to public.companies).

The id. A company whose wba_id is already a lanterN id keeps it. Any other gets
`add_companies.company_id(name, lei)`: "LN_" and the first 8 hex digits of the SHA-1 of its LEI, or of its name
without accents, case, punctuation and legal endings when it has no LEI. That is the id the same company would
have got had it been asked for through lanterN, so a company can never get two.

Permanent. A `lantern_id` already written is never changed, whatever happens to the name or the LEI later:
the script fills empty cells only. Two companies that would get the same id (the same LEI on two rows, the
same cleaned name) are both left empty and reported, never given a made-up id: a person looks at the pair.

    python lantern_ids.py [--commit]

Dry run prints the count and the first 20. Env: AIRTABLE_TOKEN, AIRTABLE_BASE; AIRTABLE_QA_TOKEN for the run
record (QA_COMMIT is set by --commit).
"""
import argparse, collections, os, re, sys, time

from add_companies import company_id

LN = re.compile(r"^LN_[0-9a-f]{8}$")
WRITE_BATCH = 10


def plan(records):
    """(to write [(record id, wba_id, lantern_id)], clashes [(lantern_id, [wba_ids])], counts). records: Airtable
    rows with wba_id, company_name, lei, lantern_id. Rows without a wba_id are not companies yet and are skipped
    (add_companies gives them an id first)."""
    c = collections.Counter()
    taken = collections.defaultdict(list)                       # lantern id -> wba ids that hold or would get it
    want = []
    for rec in records:
        f = rec.get("fields", {})
        wba = (f.get("wba_id") or "").strip()
        if not wba:
            c["no wba_id yet"] += 1
            continue
        have = (f.get("lantern_id") or "").strip()
        if have:
            c["already has one"] += 1
            taken[have].append(wba)
            continue
        new = wba if LN.match(wba) else company_id(f.get("company_name"), f.get("lei"))
        if not new:
            c["no name and no LEI"] += 1
            continue
        taken[new].append(wba)
        want.append((rec["id"], wba, new))
    clashes = sorted((i, sorted(w)) for i, w in taken.items() if len(w) > 1)
    bad = {i for i, _ in clashes}
    todo = [t for t in want if t[2] not in bad]
    c["to write"] = len(todo)
    c["left empty: the id is shared"] = len(want) - len(todo)
    return todo, clashes, c


def payloads(todo):
    for i in range(0, len(todo), WRITE_BATCH):
        yield {"records": [{"id": rid, "fields": {"lantern_id": lid}} for rid, _, lid in todo[i:i + WRITE_BATCH]]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    for v in ("AIRTABLE_TOKEN", "AIRTABLE_BASE"):
        if not os.environ.get(v):
            sys.exit(f"Set {v}.")
    import qa
    from base_health import sweep, API
    from monitor_core import airtable_request
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("lantern_ids.py")
    records = list(sweep("companies", ["wba_id", "company_name", "lei", "lantern_id"]))
    todo, clashes, c = plan(records)
    print(f"{len(records):,} rows in companies: " + ", ".join(f"{k} {v:,}" for k, v in sorted(c.items()) if v), flush=True)
    for lid, wbas in clashes:
        print(f"  shared id {lid}: {', '.join(wbas)}", flush=True)
        run.failure("DATA_CHECK", f"{len(wbas)} companies would share the lanterN id {lid} (the same LEI or the same name): {', '.join(wbas)}",
                    key=f"lantern_id_clash|{lid}", wba_id=wbas[0])
    for _, wba, lid in todo[:20]:
        print(f"  {wba} -> {lid}", flush=True)
    written = 0
    if a.commit:
        headers = {"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}", "Content-Type": "application/json"}
        for body in payloads(todo):
            airtable_request("PATCH", f"{API}/{os.environ['AIRTABLE_BASE']}/companies", headers, payload=body)   # a text field: no typecast, no link
            written += len(body["records"])
            time.sleep(0.22)
    print(f"{written:,} written" + ("" if a.commit else " (dry run: nothing written)"), flush=True)
    run.finish("ok", rows_in=len(records), rows_out=written, counters=dict(c, clashes=len(clashes)), notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
