#!/usr/bin/env python3
"""
fiscal_years.py — write each document's reporting year, read from its full text, to report_library.

For every document in the full-text store (build_fulltext.py) that is not a policy, fiscal_year.py
reads all of its pages and decides the year in which the reporting period it covers ends, with a
confidence score and the sentence that proves it. This script writes, on report_library:

    fiscal_year     the year (e.g. 2021)
    period_end      "2021-12" when the month is known, else "2021"
    year_score      0-100 confidence (see fiscal_year.py; calibrated against the analysts' years:
                    80+ was 99% right, 60-79 about 96%, below 60 not reliable)
    year_basis      which kind of evidence decided: scope / period / title / label, or none
    year_evidence   "p.<page>: <the sentence>" so anyone can check it in the PDF

The {year} formula uses fiscal_year when year_score >= 60, and falls back to the older rules
otherwise. Policies (Code of Conduct, Human Rights Policy, Policy Documents) are skipped: their
"year" is a version, not a reporting period.

The period can never end after the document was published: the latest of the upload folder or a
date in the file name, and the month the row was first seen, cap the evidence.

Queue: rows with year_basis blank (never read). --all re-reads everything (after a rule change).
Modes: dry run by default (prints the distribution and a sample); --commit writes.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE, TEXT_STORE_REPO + TEXT_STORE_TOKEN (or TEXT_STORE_DIR)
"""
import argparse, collections, gzip, json, os, sys, tempfile, time
from concurrent.futures import ProcessPoolExecutor
from urllib.parse import quote

import fiscal_year as FY

API = "https://api.airtable.com/v0"
POLICIES = {"Code of Conduct", "Human Rights Policy", "Policy Documents"}
FIELDS = ["library_id", "document_url", "file", "first_seen", "source_type_check", "source_type",
          "discard", "fiscal_year", "period_end", "year_score", "year_basis", "year_evidence"]


def airtable_rows(token, base, everything):
    from monitor_core import airtable_request
    H = {"Authorization": f"Bearer {token}"}
    formula = "NOT({discard})" if everything else "AND(NOT({discard}), {year_basis}='')"
    params = [("pageSize", "100"), ("filterByFormula", formula)] + [("fields[]", f) for f in FIELDS]
    out, offset = {}, None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", f"{API}/{base}/report_library", H, params=p).json()
        for r in j.get("records", []):
            lid = r["fields"].get("library_id")
            if lid:
                out[lid] = r
        offset = j.get("offset")
        if not offset:
            return out
        time.sleep(0.21)


def cap_for(fields):
    """Latest (year, month) the period can end in: publication evidence in the link, first seen."""
    import date_documents as D
    att = (fields.get("file") or [{}])[0]
    _, dated, folder = D.url_parts(fields.get("document_url") or "", att.get("filename") or "")
    caps = []
    if folder or dated:
        caps.append((max(folder | dated), 12))
    fs = (fields.get("first_seen") or "")[:7]
    if len(fs) == 7 and fs[4] == "-":
        caps.append((int(fs[:4]), int(fs[5:7])))
    return min(caps) if caps else None


def read_part(args):
    """Decide every wanted document whose newest copy is in this pages part."""
    path, wanted = args
    out, cur, pages = {}, None, []

    def flush():
        if cur is not None and pages:
            cap = wanted[cur]
            cap = tuple(cap) if cap else None
            y, end, score, quote, det = FY.decide(sorted(pages), cap)
            out[cur] = (y, end, score, det.get("tier") or "none", quote)

    with gzip.open(path, "rt") as fh:
        for line in fh:
            r = json.loads(line)
            if r["id"] not in wanted:
                continue
            if r["id"] != cur:
                flush(); cur, pages = r["id"], []
            pages.append((r["p"], r.get("t") or ""))
    flush()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--all", action="store_true", help="re-read every document, not only new ones")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    token, base = os.environ.get("AIRTABLE_TOKEN"), os.environ.get("AIRTABLE_BASE")
    if not (token and base):
        sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
    import text_core as tc
    rows = airtable_rows(token, base, a.all)
    print(f">>> {len(rows)} library rows to read.", flush=True)
    store = tc.open_store()
    folder = tempfile.mkdtemp(prefix="fy-")
    names = [n for n in store.assets() if tc.part_of(n)[0]]
    tc.sync_down(store, [n for n in names if n.startswith("docs-")], folder)
    index = tc.load_docs_index(folder, names)
    want = collections.defaultdict(dict)          # part -> {library_id: cap}
    skipped = collections.Counter()
    for lid, r in rows.items():
        d = index.get(lid)
        f = r["fields"]
        t = (f.get("source_type_check") or "").strip() or (f.get("source_type") or "").strip()
        if t in POLICIES:
            skipped["policy"] += 1; continue
        if not d or d.get("status") != "ok":
            skipped["no text yet"] += 1; continue
        want[d["part"]][lid] = cap_for(f)
        if a.limit and sum(len(v) for v in want.values()) >= a.limit:
            break
    n_docs = sum(len(v) for v in want.values())
    print(f">>> {n_docs} documents to read in {len(want)} parts; skipped {dict(skipped)}.", flush=True)
    tc.sync_down(store, [f"pages-{p}.jsonl.gz" for p in want], folder)
    results = {}
    jobs = [(os.path.join(folder, f"pages-{p}.jsonl.gz"), ids) for p, ids in want.items()]
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for i, out in enumerate(ex.map(read_part, jobs), 1):
            results.update(out)
            if i % 20 == 0:
                print(f"  {i}/{len(jobs)} parts, {len(results)} documents", flush=True)
    # a document in the queue with no readable pages gets basis "none" so it leaves the queue
    for ids in want.values():
        for lid in ids:
            results.setdefault(lid, (None, None, 0, "none", ""))
    bands = collections.Counter(); basis = collections.Counter(); ups = []
    for lid, (y, end, score, tier, quote) in results.items():
        bands["80+" if score >= 80 else "60-79" if score >= 60 else "<60" if y else "no evidence"] += 1
        basis[tier] += 1
        f = rows[lid]["fields"]
        new = {"fiscal_year": y, "period_end": end or "", "year_score": score if y else None,
               "year_basis": tier, "year_evidence": quote}
        if any((f.get(k) or None) != (v or None) for k, v in new.items()):
            ups.append({"id": rows[lid]["id"], "fields": new})
    lines = [f"## Fiscal years from the full text ({'written' if a.commit else 'dry run, nothing written'})", "",
             f"Documents read: {len(results)}. Rows to update: {len(ups)}.", "",
             "Confidence: " + ", ".join(f"{k} {v}" for k, v in bands.most_common()), "",
             "Decided by: " + ", ".join(f"{k} {v}" for k, v in basis.most_common()), "",
             "| library_id | year | score | basis | evidence |", "|---|---|---|---|---|"]
    for lid, (y, end, score, tier, quote) in list(results.items())[:40]:
        lines.append(f"| {lid} | {end or ''} | {score} | {tier} | {quote[:150].replace('|', '/')} |")
    out = "\n".join(lines)
    print(out, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(out + "\n")
    if not a.commit:
        print("DRY RUN. Re-run with --commit to write.")
        return
    from monitor_core import airtable_request
    H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    done = 0
    for i in range(0, len(ups), 10):
        try:
            airtable_request("PATCH", f"{API}/{base}/report_library", H, {"records": ups[i:i + 10], "typecast": True})
            done += len(ups[i:i + 10])
        except Exception as e:
            print(f">>> WARN: write failed ({e}); those rows stay queued.", flush=True)
        time.sleep(0.22)
        if done and done % 5000 == 0:
            print(f"  {done} written", flush=True)
    print(f">>> wrote {done} rows.")


if __name__ == "__main__":
    main()
