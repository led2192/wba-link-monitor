#!/usr/bin/env python3
"""
citation_coverage.py: how many of the sources the analysts cited do we hold?

year_window_eval.py (6 Nov 2026) matched only 2% of the 104,525 sources cited by validated answers to the
documents of the evidence database. That join only sees documents whose text is loaded, and an address can
differ (another host, another folder, a web page rather than a file). This measures it properly, per gold
base, per kind of source and per reporting year:

  - the kind of each cited source: a PDF (the address names a .pdf), a web page, or no address;
  - held in report_library (the Disclosure Library, every document we know, discarded ones apart): same
    company and same address; else same company and same PDF file name; else the same address under another
    company (an id mismatch, not a missing document);
  - with text in the evidence database (what the readers can read).

Counts and shares only (the report is public): no company, no address. Read-only: gold bases (GOLD_TOKEN),
report_library (AIRTABLE_TOKEN, AIRTABLE_BASE), evidence database (SUPABASE_DB_URL).
Options: --bases (default the gold bases of gold_attributes.json).
"""
import argparse, collections, json, os, sys, traceback

import qa
from gold_eval import gold_id
from year_window_eval import norm_url, file_key

HERE = os.path.dirname(os.path.abspath(__file__))


def kind_of(url):
    u = (url or "").strip().lower()
    if not u:
        return "no address"
    return "PDF" if ".pdf" in u.split("?")[0] or "/pdf" in u else "web page"


def classify(wba, url, lib):
    """Where a cited source is held. lib: {"addr": {(wba, norm): lid}, "file": {(wba, file): lid},
    "any": {norm: lid}}. Returns (status, library_id or None)."""
    n = norm_url(url)
    if not n:
        return "no address", None
    if (wba, n) in lib["addr"]:
        return "same company, same address", lib["addr"][(wba, n)]
    k = file_key(url)
    if k and (wba, k) in lib["file"]:
        return "same company, same file name", lib["file"][(wba, k)]
    if n in lib["any"]:
        return "same address, another company", lib["any"][n]
    return "not held", None


STATUSES = ["same company, same address", "same company, same file name", "same address, another company", "not held", "no address"]


def library_index(rows):
    """rows: [(library_id, wba_id, document_url)] -> the three lookups of classify."""
    lib = {"addr": {}, "file": {}, "any": {}}
    for lid, wba, url in rows:
        n = norm_url(url)
        if not n:
            continue
        lib["addr"].setdefault((wba, n), lid)
        lib["any"].setdefault(n, lid)
        k = file_key(url)
        if k:
            lib["file"].setdefault((wba, k), lid)
    return lib


def tally(cites, lib, with_text):
    """cites: [{base, wba, year, url}] -> {group: Counter}, groups by base, kind and year."""
    out = collections.defaultdict(collections.Counter)
    for c in cites:
        st, lid = classify(c["wba"], c["url"], lib)
        groups = ["All", "base " + c["base"], "kind " + kind_of(c["url"]), f"year {c['year']}"]
        for g in groups:
            t = out[g]
            t["cited"] += 1
            t[st] += 1
            t["held"] += st.startswith("same")
            t["with text"] += bool(lid and lid in with_text)
    return out


def report(t):
    pct = lambda a, b: f"{100 * a / b:.0f}%" if b else "-"
    md = ["### How many of the analysts' cited sources we hold", "",
          "Sources cited by validated answers in the gold bases. Held: in report_library (discarded rows apart) by the same "
          "company and address, the same company and PDF file name, or the same address under another company. With text: "
          "the held document's text is in the evidence database, so the readers can read it. Counts and shares only.", "",
          "| Group | Cited | Held | " + " | ".join(STATUSES) + " | With text |",
          "| --- | --: | --: |" + " --: |" * len(STATUSES) + " --: |"]
    order = (["All"] + sorted(g for g in t if g.startswith("base ")) + sorted(g for g in t if g.startswith("kind "))
             + sorted((g for g in t if g.startswith("year ")), key=lambda g: g))
    for g in order:
        c = t[g]
        if g.startswith("year ") and c["cited"] < 100:
            continue
        md.append(f"| {g} | {c['cited']:,} | {pct(c['held'], c['cited'])} | " + " | ".join(pct(c[s], c["cited"]) for s in STATUSES)
                  + f" | {pct(c['with text'], c['cited'])} |")
    return md


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bases", default="")
    a = ap.parse_args()
    run = qa.Run("citation_coverage.py")
    gold_token = os.environ["GOLD_TOKEN"].strip()
    cfg = json.load(open(os.path.join(HERE, "gold_attributes.json"), encoding="utf-8"))
    names = {v: k for k, v in cfg["bases"].items()}
    bases = [b for b in a.bases.split(",") if b] or list(cfg["bases"].values())
    from narrative_eval import read_base
    from value_eval import gold_sources
    cites, seen = [], set()
    for b in bases:
        src = gold_sources(gold_token, b)
        rows = read_base(gold_token, b, "Metadata", ["Metadata ID", "Source Link"], "AND({Data Reviewed}, {Source Link}&''!='')")
        for r in rows:
            f = r["fields"]
            mid = f.get("Metadata ID")
            wba, code, year = gold_id(mid)
            if not code or mid in seen:
                continue
            seen.add(mid)
            for i in f.get("Source Link") or []:
                if i in src:
                    cites.append({"base": names.get(b, b), "wba": wba, "year": year, "url": src[i]["url"]})
        print(f">>> {names.get(b, b)}: {len(rows):,} answers, {len(src):,} sources", flush=True)
    lib_rows = read_base(os.environ["AIRTABLE_TOKEN"].strip(), os.environ["AIRTABLE_BASE"].strip(), "report_library",
                         ["library_id", "wba_id", "document_url"], "NOT({discard})")
    lib = library_index([(r["fields"].get("library_id"), r["fields"].get("wba_id"), r["fields"].get("document_url")) for r in lib_rows])
    print(f">>> report_library: {len(lib_rows):,} rows kept", flush=True)
    with __import__("evidence_db").connect() as conn:
        cur = conn.cursor()
        cur.execute("set statement_timeout = '120s'")
        cur.execute("select library_id from public.documents where coalesce(pages, 0) > 0")
        with_text = {x for (x,) in cur.fetchall()}
    print(f">>> evidence database: {len(with_text):,} documents with text", flush=True)
    md = report(tally(cites, lib, with_text))
    open("citation_coverage.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    run.finish("ok", rows_in=len(cites))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("citation_coverage.md", "w").write("### Citation coverage: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
