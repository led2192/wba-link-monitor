#!/usr/bin/env python3
"""
archive_documents.py: free room in the Disclosure Library by discarding old documents nobody uses.

The base holds at most 500,000 records and stood at 84.5% on 9 Nov 2026 with about 1,400 documents a day
arriving. `base_room.py` counts the candidates; this script acts on them, in the one way the house rules allow:
it never deletes. It ticks `discard` with the reason ARCHIVE_REASON on every live document that meets the
rule, and the purge job (`purge_discarded.py --machine-only --days 7`) deletes them later, after archiving every
field to a GitHub Release and tombstoning each document so the harvest never brings it back. Unticking discard
within those days restores a row. The documents' text stays in the evidence database, so the reader can still
read them and a proposal can still cite them by library_id, page and URL; only the Airtable row and its attached
file go.

Rules (base_room.rules_for; a candidate is never cited by an attribute_values row, verified, imported from the
Source Library or a company's current key report):
  B  reporting year CUTOFF_YEAR (2021) or earlier and not a key type (the cautious rule: presentations, loose
     policies, certifications, interim reports, notes; the default);
  A  reporting year 2021 or earlier, any type (also the old annual and sustainability reports, which the anchor
     and the company memory may read as comparatives: the owner decides).
Dry run by default: prints the counts by type, year and company, 20 examples, and writes
archive_candidates.csv. --commit ticks the rows (10 per request). --limit caps a run.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE; AIRTABLE_QA_TOKEN for the run record.
"""
import argparse, collections, csv, os, sys, time
from datetime import datetime, timezone
from urllib.parse import quote

ARCHIVE_REASON = "Archived: before 2022, not cited"
BATCH = 10
WRITE_PAUSE = 0.22


def candidates(docs, cited, keys, today, rule="B"):
    """The live documents meeting the rule, as (record id, fields)."""
    from base_room import rules_for
    out = []
    for rid, f in docs:
        letters = rules_for(f, cited, keys, today)
        if rule in letters:
            out.append((rid, f))
    return out


def summary(cands):
    by_type = collections.Counter((f.get("source_type_check") or "(no type)") for _, f in cands)
    by_year = collections.Counter(str(f.get("year") or "?")[:4] for _, f in cands)
    by_company = collections.Counter(f.get("wba_id") or "?" for _, f in cands)
    return by_type, by_year, by_company


def report_md(cands, rule, today, committed=None):
    by_type, by_year, by_company = summary(cands)
    md = [f"# Archive candidates, rule {rule} ({today.isoformat()})", "",
          f"{len(cands):,} live documents meet rule {rule}: reporting year 2021 or earlier"
          + (", not a key type" if rule == "B" else "") + ", never cited by a value, verified, imported from the Source Library or a current key report.", "",
          "| Type | Documents |", "| --- | --: |"] + [f"| {t} | {n:,} |" for t, n in by_type.most_common()]
    md += ["", "| Reporting year | Documents |", "| --- | --: |"] + [f"| {y} | {n:,} |" for y, n in sorted(by_year.items())]
    md += ["", f"{len(by_company):,} companies; the ten with most candidates: " + ", ".join(f"{c} ({n:,})" for c, n in by_company.most_common(10)) + ".", "",
           "Twenty examples (library_id, year, type, title, URL):", ""]
    for _, f in cands[:20]:
        md.append(f"- {f.get('library_id')} | {f.get('year')} | {f.get('source_type_check') or '(no type)'} | {(f.get('doc_label') or '')[:90]} | {f.get('document_url') or ''}")
    if committed is None:
        md += ["", "Dry run: nothing written. --commit ticks discard with the reason \"" + ARCHIVE_REASON + "\"; the purge job deletes them after 7 days (machine-only), archiving every field first."]
    else:
        md += ["", f"Written: discard ticked on {committed:,} rows with the reason \"{ARCHIVE_REASON}\". Untick to restore; the purge job deletes them after 7 days."]
    return "\n".join(md) + "\n"


def write_csv(cands, path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["record_id", "library_id", "wba_id", "year", "source_type_check", "doc_label", "document_url"])
        for rid, f in cands:
            w.writerow([rid, f.get("library_id"), f.get("wba_id"), f.get("year"), f.get("source_type_check"), f.get("doc_label"), f.get("document_url")])


def payloads(cands):
    """The PATCH bodies, BATCH records each: discard ticked and the reason. typecast lets Airtable add the reason
    to the select's choices on the first write (no link field is touched)."""
    for i in range(0, len(cands), BATCH):
        yield {"records": [{"id": rid, "fields": {"discard": True, "discard_reason": ARCHIVE_REASON}} for rid, _ in cands[i:i + BATCH]],
               "typecast": True}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rule", choices=["A", "B"], default="B")
    ap.add_argument("--limit", type=int, default=0, help="at most this many rows (0 = all)")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--out", default="archive_candidates")
    a = ap.parse_args()
    if not (os.environ.get("AIRTABLE_TOKEN") and os.environ.get("AIRTABLE_BASE")):
        sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
    import qa
    from base_health import req, sweep, API, BASE
    from base_room import DOC_FIELDS, key_types
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("archive_documents.py")
    today = datetime.now(timezone.utc).date()
    t0 = time.time()
    cited = {rec.get("fields", {}).get("library_id") for rec in sweep("attribute_values", ["library_id"])} - {None}
    docs = [(rec["id"], rec.get("fields", {}) | {"created_time": rec.get("createdTime")})
            for rec in sweep("report_library", DOC_FIELDS + ["doc_label", "document_url"])]
    print(f"  {len(docs):,} documents, {len(cited):,} cited, read in {time.time() - t0:.0f}s", flush=True)
    cands = candidates(docs, cited, key_types(), today, a.rule)
    cands.sort(key=lambda x: (str(x[1].get("year") or ""), x[1].get("wba_id") or "", x[1].get("library_id") or ""))
    if a.limit:
        cands = cands[:a.limit]
    write_csv(cands, a.out + ".csv")
    committed = None
    if a.commit and cands:
        committed = 0
        url = f"{API}/{BASE}/{quote('report_library')}"
        for body in payloads(cands):
            req("PATCH", url, json=body)
            committed += len(body["records"])
            time.sleep(WRITE_PAUSE)
            if committed % 1000 == 0:
                print(f"  {committed:,} rows ticked", flush=True)
    md = report_md(cands, a.rule, today, committed)
    open(a.out + ".md", "w", encoding="utf-8").write(md)
    print(md)
    run.finish("ok", rows_in=len(docs), rows_out=committed or 0,
               counters={"candidates": len(cands), "rule": a.rule, "committed": committed or 0},
               notes=f"rule {a.rule}: {len(cands):,} candidates" + (f", {committed:,} ticked" if committed else ", dry run"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
