#!/usr/bin/env python3
"""
import_source_library.py — bring the reports analysts collected in the Source Library bases into
the Disclosure Library, without duplicating anything we already hold.

The Source Library bases (2024, 2025, ...) are human-curated: an analyst found each report, uploaded
the PDF, and set its type and year. Their "Source Link" is usually the page the report was found on,
not the PDF, so the file to bring over is the attachment (Source File), not the link.

What is imported (planned per Source Library record):
  reports   Sustainability / ESG / CSR / Impact / Integrated / Annual / 10-K of year >= --min-year
  policies  Code of Conduct and Human Rights Policy: the newest one per company, ONLY when the
            Disclosure Library holds no document of that type for the company at all

Never imported (the duplicate guards, all checked against the live Disclosure Library):
  1. same company + same file size in bytes (the same file; discarded rows count as held)
  2. same company + same file name
  3. reports: we already hold a document of the same family for that company in that year or
     the year before/after (it may be the same report with a different file, or a year that is
     off by one; those are listed in the plan as "review", never added)
  4. files Edi deleted by hand (state/deleted_by_hand.jsonl.gz: same company + size or name)
  5. inside the import itself: one document per company + type + year, preferring the newest
     Source Library base, then the largest file

Each new report_library row is an upload (same identity rule as inbox.py's add_file):
  library_id = wba + sha1("upload:wba:filename:size")[:10]; no document_url; found_on = the
  Source Library link; file = the Source Library PDF (Airtable copies it); doc_year = the analyst's
  year (year in which the fiscal year ends), doc_year_from = "source library"; source_type = the
  analyst's type; Reviewer notes say which base and record it came from.

Modes: dry run by default (prints the plan and a summary); --commit creates the rows.
Options: --bases 2025,2024  --min-year 2023  --limit N (rows to create)  --plan-out PATH
Safety: the Source Library bases are only ever READ, and that is guaranteed twice:
  - they are read with a separate token, SOURCE_LIBRARY_TOKEN, that should have ONLY the
    data.records:read scope and ONLY the Source Library bases: Airtable itself then refuses any write;
  - the only function that touches them (get_all with sl=True) refuses anything but GET, and the one
    write in this script goes to AIRTABLE_BASE, which must not be a Source Library base.
Env: AIRTABLE_TOKEN + AIRTABLE_BASE (Disclosure Library), SOURCE_LIBRARY_TOKEN (read-only, Source Library bases)
"""
import argparse, collections, csv, gzip, hashlib, json, os, sys, time
from datetime import date
from urllib.parse import quote, unquote, urlsplit

API = "https://api.airtable.com/v0"

# Source Library bases: base id, tables and the field names each one uses
SOURCE_LIBRARIES = {
    "2025": {"base": "appu5pLRjmq7PC4V3", "sources": "Sources", "companies": "Companies", "co_id": "WBA ID",
             "link": "Source Link", "type": "Source Type", "year": "Source Year", "file": "Source File",
             "name": "Source Name", "wba": "WBA ID"},
    "2024": {"base": "appsrc5xpZH12ezzP", "sources": "Sources", "companies": "Companies", "co_id": "wba-id",
             "link": "Source Link", "type": "Source Type", "year": "Source Year", "file": "Source File",
             "name": "Source Name", "wba": "WBA ID"},
}
FAMILY = {"Sustainability Report": "SUST", "ESG Report": "SUST", "CSR Report": "SUST", "Impact Report": "SUST",
          "Integrated Report": "INT", "Annual Report": "ANN", "10K Form": "ANN",
          "Code of Conduct": "COC", "Human Rights Policy": "HRP"}
# which Disclosure Library types already cover a Source Library family
HELD_BY = {"SUST": {"Sustainability Report", "ESG Report", "CSR Report", "Impact Report"},
           "INT": {"Integrated Report", "Annual Report"},
           "ANN": {"Annual Report", "10K Form", "Integrated Report"},
           "COC": {"Code of Conduct"}, "HRP": {"Human Rights Policy"}}
REPORTS = {"SUST", "INT", "ANN"}


SL_BASES = {c["base"] for c in SOURCE_LIBRARIES.values()}


def get_all(token, base, table, fields, formula=None):
    """GET only. This is the only function that ever addresses a Source Library base."""
    from monitor_core import airtable_request
    H = {"Authorization": f"Bearer {token}"}
    params = [("pageSize", "100")] + [("fields[]", f) for f in fields]
    if formula:
        params.append(("filterByFormula", formula))
    out, offset = [], None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", f"{API}/{base}/{quote(table)}", H, params=p).json()
        out += j.get("records", [])
        offset = j.get("offset")
        if not offset:
            return out
        time.sleep(0.21)


def fname_of(u):
    return unquote(urlsplit((u or "").split("?")[0]).path.rstrip("/").rsplit("/", 1)[-1]).lower()


def read_sources(token, tag, min_year):
    """Reads one Source Library base (GET requests only, with the read-only token)."""
    c = SOURCE_LIBRARIES[tag]
    cos = get_all(token, c["base"], c["companies"], [c["co_id"]])
    wba_of = {r["id"]: (r["fields"].get(c["co_id"]) or "").strip() for r in cos}
    types = ",".join(f"{{{c['type']}}}='{t}'" for t in FAMILY)
    recs = get_all(token, c["base"], c["sources"], [c[k] for k in ("link", "type", "year", "file", "name", "wba")],
                   f"OR({types})")
    out = []
    for r in recs:
        f = r["fields"]
        t = f.get(c["type"]); att = (f.get(c["file"]) or [None])[0]
        try:
            y = int(str(f.get(c["year"]) or "0")[:4])
        except ValueError:
            y = 0
        links = f.get(c["wba"]) or []
        wba = wba_of.get(links[0], "") if links else ""
        if not (t in FAMILY and att and att.get("size") and wba):
            continue
        fam = FAMILY[t]
        if fam in REPORTS and y < min_year:
            continue
        out.append({"src": tag, "rec": r["id"], "wba": wba, "type": t, "fam": fam, "year": y,
                    "link": (f.get(c["link"]) or "").strip(), "name": f.get(c["name"]) or "",
                    "file_url": att["url"], "filename": att.get("filename") or "file.pdf", "size": att["size"]})
    return out


def read_library(token, base):
    rows = get_all(token, base, "report_library",
                   ["wba_id", "file", "document_url", "year", "source_type_check", "source_type"])
    held = {"size": set(), "name": set(), "fam_years": collections.defaultdict(set),
            "types": collections.defaultdict(set)}
    for r in rows:
        f = r["fields"]; w = (f.get("wba_id") or "").strip()
        if not w:
            continue
        for a in (f.get("file") or [])[:1]:
            if a.get("size"):
                held["size"].add((w, a["size"]))
            if a.get("filename"):
                held["name"].add((w, a["filename"].lower()))
        n = fname_of(f.get("document_url"))
        if len(n) > 8:
            held["name"].add((w, n))
        t = (f.get("source_type_check") or "").strip() or (f.get("source_type") or "").strip()
        held["types"][w].add(t)
        try:
            y = int(float(f.get("year")))
        except (TypeError, ValueError):
            y = None
        if y:
            for fam, ts in HELD_BY.items():
                if t in ts:
                    held["fam_years"][(w, fam)].add(y)
    return held, len(rows)


def read_deleted():
    out = set()
    try:
        for l in gzip.open("state/deleted_by_hand.jsonl.gz", "rt"):
            d = json.loads(l); f = d.get("fields") or {}
            w = (f.get("wba_id") or "").strip()
            for a in (f.get("file") or [])[:1]:
                if a.get("size"):
                    out.add((w, a["size"]))
                if a.get("filename"):
                    out.add((w, a["filename"].lower()))
    except FileNotFoundError:
        pass
    return out


def plan(sources, held, deleted, order):
    """[(source, verdict)] verdict in add / skip:<reason> / review:<reason>."""
    rank = {tag: i for i, tag in enumerate(order)}           # earlier in --bases = preferred
    sources = sorted(sources, key=lambda s: (rank[s["src"]], -s["size"]))
    seen_size, seen_slot, out = set(), set(), []
    newest_policy = {}
    for s in sources:
        if s["fam"] not in REPORTS:
            k = (s["wba"], s["type"])
            if k not in newest_policy or s["year"] > newest_policy[k]["year"]:
                newest_policy[k] = s
    for s in sources:
        w, fam, y = s["wba"], s["fam"], s["year"]
        if (w, s["size"]) in held["size"] or (w, s["filename"].lower()) in held["name"]:
            out.append((s, "skip:already held (same file)")); continue
        if (w, s["size"]) in deleted or (w, s["filename"].lower()) in deleted:
            out.append((s, "skip:deleted by hand")); continue
        if (w, s["size"]) in seen_size:
            out.append((s, "skip:same file in another Source Library")); continue
        if fam in REPORTS:
            have = held["fam_years"].get((w, fam), set())
            if y in have:
                out.append((s, "skip:we hold this report type for that year")); continue
            if y - 1 in have or y + 1 in have:
                out.append((s, "review:we hold this report type for the year before or after")); continue
            slot = (w, s["type"], y)
        else:
            if s["type"] in held["types"].get(w, set()):
                out.append((s, "skip:we hold this policy type")); continue
            if newest_policy.get((w, s["type"])) is not s:
                out.append((s, "skip:a newer version of this policy is imported")); continue
            slot = (w, s["type"])
        if slot in seen_slot:
            out.append((s, "skip:one per company, type and year")); continue
        seen_slot.add(slot); seen_size.add((w, s["size"]))
        out.append((s, "add"))
    return out


def row_for(s, names):
    key = f"upload:{s['wba']}:{s['filename'].lower()}:{s['size']}"
    return {"library_id": f"{s['wba']}-{hashlib.sha1(key.encode()).hexdigest()[:10]}",
            "wba_id": s["wba"], "company_name": names.get(s["wba"], ""),
            "file": [{"url": s["file_url"], "filename": s["filename"]}],
            **({"found_on": s["link"]} if s["link"] else {}),
            "doc_year": str(s["year"]), "doc_year_from": "source library",
            "source_type": s["type"], "first_seen": date.today().isoformat(),
            "Reviewer notes": (f"From Source Library {s['src']} (record {s['rec']}): {s['type']} {s['year']}"
                               f" — \"{s['name']}\". Type and year set by an analyst.")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bases", default="2025,2024")
    ap.add_argument("--min-year", type=int, default=2023)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--plan-out", default="source_library_plan.csv")
    a = ap.parse_args()
    token, base = os.environ.get("AIRTABLE_TOKEN"), os.environ.get("AIRTABLE_BASE")
    sl_token = os.environ.get("SOURCE_LIBRARY_TOKEN")
    if not (token and base and sl_token):
        sys.exit("Set AIRTABLE_TOKEN, AIRTABLE_BASE and SOURCE_LIBRARY_TOKEN (read-only).")
    if base in SL_BASES:
        sys.exit("AIRTABLE_BASE is a Source Library base: refusing to run (this script never writes there).")
    order = [b.strip() for b in a.bases.split(",") if b.strip()]
    sources = []
    for tag in order:
        try:
            got = read_sources(sl_token, tag, a.min_year)
        except Exception as e:
            sys.exit(f"Cannot read Source Library {tag} ({e}). The token needs read access to that base.")
        print(f">>> Source Library {tag}: {len(got)} documents in scope.", flush=True)
        sources += got
    held, n = read_library(token, base)
    print(f">>> Disclosure Library: {n} rows read.", flush=True)
    items = plan(sources, held, read_deleted(), order)
    adds = [s for s, v in items if v == "add"]
    if a.limit:
        adds = adds[:a.limit]
    verdicts = collections.Counter(v for _, v in items)
    by = collections.Counter((s["type"], s["year"]) for s in adds)
    mb = sum(s["size"] for s in adds) / 2 ** 20
    with open(a.plan_out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["verdict", "base", "record", "wba_id", "type", "year", "name", "filename", "size_mb", "link"])
        for s, v in items:
            w.writerow([v, s["src"], s["rec"], s["wba"], s["type"], s["year"], s["name"], s["filename"],
                        round(s["size"] / 2 ** 20, 1), s["link"]])
    lines = [f"## Source Library import ({'written' if a.commit else 'dry run, nothing written'})", "",
             f"Documents in scope: {len(items)}. To add: {len(adds)} ({mb:,.0f} MB) for "
             f"{len({s['wba'] for s in adds})} companies.", "",
             "Verdicts: " + "; ".join(f"{k} {v}" for k, v in verdicts.most_common()), "",
             "To add by type and year: " + "; ".join(f"{t} {y}: {v}" for (t, y), v in sorted(by.items()))]
    out = "\n".join(lines)
    print(out, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(out + "\n")
    if not a.commit:
        print("DRY RUN. Re-run with --commit to create the rows.")
        return
    from monitor_core import airtable_request
    names = {r["fields"].get("wba_id"): r["fields"].get("company_name", "")
             for r in get_all(token, base, "companies", ["wba_id", "company_name"])}
    url = f"{API}/{base}/report_library"
    assert base not in SL_BASES and "report_library" in url    # the one write: Disclosure Library only
    H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    made = 0
    for i in range(0, len(adds), 10):
        recs = [{"fields": row_for(s, names)} for s in adds[i:i + 10]]
        try:
            airtable_request("POST", url, H, {"records": recs, "typecast": True})
            made += len(recs)
        except Exception as e:
            print(f">>> WARN: batch failed ({e}); continuing.", flush=True)
        time.sleep(0.25)
        if made and made % 500 == 0:
            print(f"  {made} created", flush=True)
    print(f">>> created {made} rows. The nightly chain reads their covers and hashes them.")


if __name__ == "__main__":
    main()
