#!/usr/bin/env python3
"""
dedupe_library.py — discard duplicate documents and non-disclosures in report_library (never deletes).

Why it exists: the harvester's library_id is keyed on the URL, but the same file often reaches the
library under several URLs (per-visit tokens on IR platforms, mirror hosts, the same PDF in two CMS
folders). In October 2026 that left ~53k duplicate rows in a 338k-row table, each one costing an
attachment, cover-text extraction and AI classification credits, and each one a row an analyst might
open. harvest_reports.py now refuses most of them at the door (doc_key in ids.py); this script is the
second line, run nightly after the attach step, and also the tool for the one-off backlog cleanup.

Two rows of the same company are the same document when EITHER:
  A. URL signal: same doc_id (ids.doc_id: the URL with per-visit tokens stripped) and their files
     do not disagree (same byte size, or one of them has no file yet); OR
  B. content signal: both files attached, identical byte size, identical page_count, and either the
     same file name in the URL or the same doc_label (the generic "| Document" fallback label does
     not count). Different file name AND different label is left alone: too weak to act on
     without a person.
  C. same file (only with --hash-signal): identical content_hash, the SHA-256 of the file bytes
     written by build_cover_text.py. Exact by construction: two rows of the same company with the
     same hash hold the same file, whatever their URLs or titles say. Off by default until Edi has
     checked a dry run (signal "same file" in dedupe_log.csv).

Within each cluster one row is kept, chosen in this order: verified, curated title, has a file,
classified (source_type_check set), oldest created_time (which keeps the original first_seen). The
others get discard = true and discard_reason = "Duplicate of another document". Rules that protect
human work: a verified row is never discarded; a cluster with two verified rows is skipped; rows
already discarded are not touched (and still count as "seen" for the keeper choice).

Second pass, non-disclosures (reason "Not a disclosure"): documents that are never ESG evidence,
recognised by their title (doc_label, which comes from the document cover). Agreed with Edi in
October 2026, ~14k rows at the time:
  - SEC insider filings: Forms 3/4/5 (statement of beneficial ownership), Form 144, Schedule 13D/13G
  - earnings and results call transcripts (a transcript whose title mentions sustainability, ESG,
    climate, responsibility, impact or human rights is kept)
A title is the only evidence used, so a row with no title yet waits for the next night. Verified
rows and rows with a curated title are never touched. Add a family by adding one entry to
NON_DISCLOSURES; the dry run prints how many rows each rule would hit.

Every discard is logged to dedupe_log.csv (discarded record, keeper record, signal) so any of them
can be reverted by unticking discard.

Modes:
  --source airtable      read the live table (default); dry-run unless --commit
  --source jsonl PATH    plan from a local export (no writes); used for the October backlog
  --commit               write the discards
  --plan-out PATH        write the plan as CSV (default dedupe_log.csv)
  --hash-signal          also cluster rows of the same company with identical content_hash

Env: AIRTABLE_TOKEN, AIRTABLE_BASE, AIRTABLE_LIBRARY_TABLE (default report_library)
"""
import argparse, collections, csv, json, os, re, sys, time
from urllib.parse import quote, urlsplit

from ids import doc_id

API = "https://api.airtable.com/v0"
REASON = "Duplicate of another document"
REASON_JUNK = "Not a disclosure"

# (rule name, pattern that must match the title, pattern that vetoes it). Titles only.
_ESG_VETO = r"sustainab|\besg\b|climate|responsib|impact|human rights|csr|environment"
NON_DISCLOSURES = [
    ("sec_insider_form_3_4_5",
     r"statement of changes in beneficial ownership|initial statement of beneficial ownership|"
     r"\bform [345]\b.*beneficial ownership", None),
    ("sec_form_144", r"\bform 144\b|notice of proposed sale of securities", None),
    ("sec_schedule_13d_13g", r"schedule 13[dg]\b|\bsc 13[dg]\b", None),
    ("call_transcript", r"transcript", _ESG_VETO),
]
_JUNK = [(n, re.compile(p, re.I), re.compile(v, re.I) if v else None) for n, p, v in NON_DISCLOSURES]
FIELDS = ["library_id", "wba_id", "document_url", "page_count", "doc_label", "discard",
          "verified", "curated_doc_title", "source_type_check", "file", "created_time", "content_hash"]
_SHA = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------- input
def _row(rec_id, f, created):
    files = f.get("file") or []
    size = files[0].get("size") if files else None
    wba = f.get("wba_id") or str(f.get("library_id", "")).rsplit("-", 1)[0]
    stc = f.get("source_type_check")
    return {"id": rec_id, "lid": f.get("library_id", ""), "wba": wba,
            "url": f.get("document_url", "") or "", "pages": f.get("page_count"),
            "label": (f.get("doc_label") or "").strip().lower(), "size": size,
            "discard": bool(f.get("discard")), "verified": bool(f.get("verified")),
            "curated": bool(f.get("curated_doc_title")), "typed": bool(stc),
            "ct": f.get("created_time") or created or "",
            "sha": (f.get("content_hash") or "").strip().lower()}


def from_jsonl(path):
    """Compact export used for the October 2026 backlog (one JSON object per record)."""
    out = []
    for line in open(path):
        r = json.loads(line)
        files = r.get("file") or []
        out.append(_row(r["id"], {
            "library_id": r.get("lid"), "wba_id": r.get("wba"), "document_url": r.get("url"),
            "page_count": r.get("pages"), "doc_label": r.get("label"), "discard": r.get("discard"),
            "verified": r.get("verified"), "curated_doc_title": r.get("curated"),
            "source_type_check": r.get("stype"), "file": files}, r.get("ct")))
    return out


def _request(method, url, headers, payload=None, params=None, tries=6):
    import requests
    delay = 2
    for attempt in range(1, tries + 1):
        try:
            r = requests.request(method, url, headers=headers, json=payload, params=params, timeout=60)
        except requests.exceptions.RequestException:
            if attempt == tries: raise
            time.sleep(delay); delay = min(delay * 2, 30); continue
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == tries: r.raise_for_status()
            time.sleep(delay); delay = min(delay * 2, 30); continue
        r.raise_for_status()
        return r


def from_airtable(base, token, table, formula=None):
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}"}
    params = [("pageSize", "100")] + [("fields[]", f) for f in FIELDS]
    if formula:
        params.append(("filterByFormula", formula))
    out = []; offset = None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = _request("GET", url, headers, params=p).json()
        for rec in j.get("records", []):
            out.append(_row(rec["id"], rec.get("fields", {}), rec.get("createdTime")))
        offset = j.get("offset"); time.sleep(0.21)
        if not offset: break
    return out


# ---------------------------------------------------------------- clustering
class DSU:
    def __init__(self): self.p = {}
    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]; x = self.p[x]
        return x
    def union(self, a, b): self.p[self.find(a)] = self.find(b)


# doc_label falls back to "<year> | <company> | Document" when no title was read; that label is
# shared by unrelated files, so it never counts as evidence on its own.
_GENERIC_LABEL = re.compile(r"\|\s*document( \d{4})?\s*$", re.I)


def _fname(u):
    return urlsplit(u.split("?", 1)[0]).path.rstrip("/").rsplit("/", 1)[-1].lower()


def plan(rows, hash_signal=False):
    """Returns list of (discard_row, keeper_row, signal)."""
    by_id = {r["id"]: r for r in rows}
    dsu = DSU(); signal = {}

    # A. URL signal, split by file size so a re-saved version with different bytes stays separate.
    by_doc = collections.defaultdict(list)
    for r in rows:
        by_doc[doc_id(r["wba"], r["url"])].append(r)
    for grp in by_doc.values():
        if len(grp) < 2: continue
        sizes = {r["size"] for r in grp if r["size"]}
        anchor = {}
        for r in grp:
            if r["size"]:
                a = anchor.setdefault(r["size"], r["id"]); dsu.union(r["id"], a)
        if not anchor: 
            for r in grp[1:]: dsu.union(r["id"], grp[0]["id"])
            first = grp[0]["id"]
        else:
            # rows without a file join the largest size bucket of their URL group
            biggest = max(sizes, key=lambda s: sum(1 for r in grp if r["size"] == s))
            first = anchor[biggest]
            for r in grp:
                if not r["size"]: dsu.union(r["id"], first)
        for r in grp: signal.setdefault(r["id"], "url")

    # B. content signal
    by_content = collections.defaultdict(list)
    for r in rows:
        if r["size"] and r["pages"]:
            by_content[(r["wba"], r["size"], r["pages"])].append(r)
    for grp in by_content.values():
        if len(grp) < 2: continue
        for i, a in enumerate(grp):
            for b in grp[i + 1:]:
                if dsu.find(a["id"]) == dsu.find(b["id"]): continue
                same_label = (a["label"] and a["label"] == b["label"]
                              and not _GENERIC_LABEL.search(a["label"]))
                if _fname(a["url"]) == _fname(b["url"]) or same_label:
                    dsu.union(a["id"], b["id"])
                    signal[a["id"]] = signal.get(a["id"], "content")
                    signal[b["id"]] = signal.get(b["id"], "content")

    # C. same file bytes (exact). Same company only: a parent and a subsidiary may legitimately
    # both hold the group report.
    if hash_signal:
        by_sha = collections.defaultdict(list)
        for r in rows:
            if _SHA.match(r.get("sha") or ""):
                by_sha[(r["wba"], r["sha"])].append(r)
        for grp in by_sha.values():
            for b in grp[1:]:
                if dsu.find(grp[0]["id"]) != dsu.find(b["id"]):
                    dsu.union(b["id"], grp[0]["id"])
                    for x in (grp[0], b):
                        signal.setdefault(x["id"], "same file")

    clusters = collections.defaultdict(list)
    for r in rows:
        if r["id"] in dsu.p: clusters[dsu.find(r["id"])].append(r)

    out = []; skipped = 0
    for grp in clusters.values():
        if len(grp) < 2: continue
        if sum(1 for r in grp if r["verified"]) > 1:
            skipped += 1; continue
        keeper = sorted(grp, key=lambda r: (not r["verified"], not r["curated"], not r["size"],
                                            not r["typed"], r["ct"], r["id"]))[0]
        for r in grp:
            if r is keeper or r["discard"] or r["verified"]: continue
            out.append((r, keeper, signal.get(r["id"], "url"), REASON))
    return out, skipped


def plan_junk(rows, already):
    """Rows whose title marks them as a non-disclosure. Returns [(row, None, rule, reason)]."""
    out = []
    for r in rows:
        if r["discard"] or r["verified"] or r["curated"] or r["id"] in already or not r["label"]:
            continue
        for name, pat, veto in _JUNK:
            if pat.search(r["label"]) and not (veto and veto.search(r["label"])):
                out.append((r, None, name, REASON_JUNK)); break
    return out


# ---------------------------------------------------------------- output
def write_plan(items, path):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["record_id", "library_id", "wba_id", "keeper_record_id", "keeper_library_id",
                    "signal", "reason", "document_url", "keeper_url", "title"])
        for r, k, s, why in items:
            w.writerow([r["id"], r["lid"], r["wba"], k["id"] if k else "", k["lid"] if k else "", s,
                        why, r["url"], k["url"] if k else "", r["label"]])


def commit(base, token, table, items):
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    for i in range(0, len(items), 10):
        recs = [{"id": r["id"], "fields": {"discard": True, "discard_reason": why}}
                for r, _, _, why in items[i:i + 10]]
        _request("PATCH", url, headers, payload={"records": recs, "typecast": True})
        time.sleep(0.22)
        if (i // 10) % 200 == 0:
            print(f"  ... {i + len(recs)}/{len(items)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["airtable", "jsonl"], default="airtable")
    ap.add_argument("--jsonl")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--plan-out", default="dedupe_log.csv")
    ap.add_argument("--hash-signal", action="store_true")
    a = ap.parse_args()

    if a.source == "jsonl":
        rows = from_jsonl(a.jsonl)
    else:
        base = os.environ.get("AIRTABLE_BASE"); token = os.environ.get("AIRTABLE_TOKEN")
        if not (base and token): sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
        rows = from_airtable(base, token, os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library"))

    dups, skipped = plan(rows, hash_signal=a.hash_signal)
    junk = plan_junk(rows, {r["id"] for r, _, _, _ in dups})
    items = dups + junk
    write_plan(items, a.plan_out)
    live = sum(1 for r in rows if not r["discard"])
    print(f"{len(rows)} rows read, {live} live. Duplicates to discard: {len(dups)} "
          f"| by signal: {dict(collections.Counter(s for _, _, s, _ in dups))} | "
          f"clusters skipped (two verified rows): {skipped}.")
    print(f"Non-disclosures to discard: {len(junk)} | by rule: "
          f"{dict(collections.Counter(s for _, _, s, _ in junk))}.")
    print(f"Total {len(items)} ({100 * len(items) / max(live, 1):.1f}% of live). Plan: {a.plan_out}")

    if a.commit and a.source == "airtable":
        commit(os.environ["AIRTABLE_BASE"], os.environ["AIRTABLE_TOKEN"],
               os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library"), items)
        print(f"Discarded {len(dups)} duplicates and {len(junk)} non-disclosures.")
    elif a.source == "airtable":
        print("DRY-RUN. Re-run with --commit to write the discards.")


if __name__ == "__main__":
    main()
