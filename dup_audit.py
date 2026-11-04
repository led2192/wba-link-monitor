#!/usr/bin/env python3
"""
dup_audit.py: documents in the library that are the same report, or one inside another, under one company.

Proposal 5 of the information retrieval review (4 Nov 2026). The same report reaches the library by several
routes (the company's site, a CDN copy, the Source Library, web search, a regulator filing), and a section
published on its own ("Sustainability Statement 2024") sits next to the annual report that holds it. Each copy
is read, counted and offered to the readers again, and two copies of one report can look like two sources.

How: every page with text in the evidence database gets a fingerprint (its words, lower case, punctuation and
spacing ignored). Shingling at page level: two documents of one company that share most fingerprints are the
same text, whatever their address or title. Exact page fingerprints, not MinHash: comparing within a company
is small enough to be exact, and a page that changed by one figure is a real difference worth keeping apart.

Relations, per pair of documents of one company (pages of fewer than 40 words left out: covers, blanks):
  same          at least 90% of the pages of each are in the other;
  contained     at least 80% of the smaller one's pages are in the larger one (a section, an excerpt);
  overlap       at least 30% of the smaller one's pages, but neither of the above (a re-issue, an annex).

Read-only against the library and the evidence database. Writes near_duplicates.csv (one row per pair) and near_duplicates.md. Nothing is discarded: a
person, or a later pull request, decides what to do with each relation (DOC_DUPLICATE in Pipeline QA records a
discard already made, so the audit does not write it). Env: SUPABASE_DB_URL; AIRTABLE_QA_TOKEN (the run record).
Options: --limit N companies (0 = all), --wba ids, --budget minutes.
"""
import argparse, collections, csv, os, re, sys, time, traceback

import qa

MIN_WORDS = 40
SAME, CONTAINED, OVERLAP = 0.9, 0.8, 0.3
WORD = re.compile(r"[^\W_]+")


def fingerprint(body):
    """The page's words, lower case, joined by single spaces (None for a page under MIN_WORDS words)."""
    w = WORD.findall((body or "").lower())
    return " ".join(w) if len(w) >= MIN_WORDS else None


def relation(na, nb, shared):
    """'same', 'contained', 'overlap' or None, from the pages with text of each document and the pages shared."""
    if not na or not nb or not shared:
        return None
    small = min(na, nb)
    if shared / na >= SAME and shared / nb >= SAME:
        return "same"
    if shared / small >= CONTAINED:
        return "contained"
    if shared / small >= OVERLAP:
        return "overlap"
    return None


def pairs_of(prints):
    """prints: {library_id: set of page fingerprints} for one company. Returns [(a, b, shared)] for every pair
    sharing at least one page, a < b. Inverted index on the fingerprints, so only pairs that share pages are
    looked at."""
    holders = collections.defaultdict(set)
    for lid, fs in prints.items():
        for f in fs:
            holders[f].add(lid)
    shared = collections.Counter()
    for lids in holders.values():
        if 1 < len(lids) <= 50:                       # a page in 50 documents is boilerplate, not a duplicate
            ls = sorted(lids)
            for i in range(len(ls)):
                for j in range(i + 1, len(ls)):
                    shared[(ls[i], ls[j])] += 1
    return [(a, b, n) for (a, b), n in shared.items()]


def keep_which(a, b):
    """Which of two 'same' documents to keep: the one with a web address, then more pages, then a typed one."""
    score = lambda d: (bool(d.get("url")), d.get("pages") or 0, (d.get("doc_type") or "UNKNOWN") != "UNKNOWN")
    return a if score(a) >= score(b) else b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="companies (0 = all)")
    ap.add_argument("--wba", default="", help="wba ids, comma-separated")
    ap.add_argument("--budget", type=float, default=150, help="minutes; then it reports what it has")
    a = ap.parse_args()
    run = qa.Run("dup_audit.py")
    import psycopg
    t0, cut = time.time(), False
    rows, counts = [], collections.Counter()
    with __import__("evidence_db").connect() as conn:
        cur = conn.cursor()
        cur.execute("set statement_timeout = '120s'")
        if a.wba:
            companies = [w.strip() for w in a.wba.split(",") if w.strip()]
        else:
            cur.execute("select wba_id from public.documents where wba_id is not null group by wba_id "
                        "having count(*) > 1 order by wba_id")
            companies = [r[0] for r in cur.fetchall()]
        if a.limit:
            companies = companies[:a.limit]
        print(f">>> {len(companies):,} companies with more than one document", flush=True)
        for i, wba in enumerate(companies, 1):
            if time.time() - t0 > a.budget * 60:
                cut = True
                break
            if i % 100 == 0:
                print(f"  {i:,}/{len(companies):,} companies, {len(rows):,} pairs", flush=True)
            try:
                cur.execute("select library_id, company, doc_type, year, label, url from public.documents where wba_id = %s", (wba,))
                docs = {r[0]: {"library_id": r[0], "company": r[1], "doc_type": r[2], "year": r[3], "label": r[4], "url": r[5]}
                        for r in cur.fetchall()}
                cur.execute("select p.library_id, p.body from public.pages p join public.documents d using (library_id) "
                            "where d.wba_id = %s", (wba,))
                prints = collections.defaultdict(set)
                for lid, body in cur:
                    f = fingerprint(body)
                    if f:
                        prints[lid].add(f)
            except Exception as e:
                conn.rollback(); cur.execute("set statement_timeout = '120s'")
                counts[f"company not read ({type(e).__name__})"] += 1
                continue
            counts["companies read"] += 1
            counts["documents with text"] += len(prints)
            for lid in prints:
                docs.setdefault(lid, {"library_id": lid})["pages"] = len(prints[lid])
            for x, y, n in pairs_of(prints):
                rel = relation(len(prints[x]), len(prints[y]), n)
                if not rel:
                    continue
                A, B = docs[x], docs[y]
                if rel != "same" and len(prints[x]) < len(prints[y]):
                    A, B = B, A                                 # A is the larger: B is inside or overlaps A
                elif rel == "same" and keep_which(A, B) is B:
                    A, B = B, A                                 # A is the one to keep
                counts[rel] += 1
                rows.append({"relation": rel, "wba_id": wba, "company": A.get("company") or B.get("company") or "",
                             "shared_pages": n,
                             "a_library_id": A["library_id"], "a_label": A.get("label") or "", "a_type": A.get("doc_type") or "",
                             "a_year": A.get("year") or "", "a_pages": A.get("pages", 0), "a_url": A.get("url") or "",
                             "b_library_id": B["library_id"], "b_label": B.get("label") or "", "b_type": B.get("doc_type") or "",
                             "b_year": B.get("year") or "", "b_pages": B.get("pages", 0), "b_url": B.get("url") or ""})
    order = {"same": 0, "contained": 1, "overlap": 2}
    rows.sort(key=lambda r: (order[r["relation"]], r["company"], -r["shared_pages"]))
    with open("near_duplicates.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["relation"])
        w.writeheader(); w.writerows(rows)
    by = lambda rel: [r for r in rows if r["relation"] == rel]
    companies_with = lambda rel: len({r["wba_id"] for r in by(rel)})
    extra = len({r["b_library_id"] for r in by("same")})
    md = ["### Near-duplicate documents (page fingerprints, within each company)", "",
          f"{counts['companies read']:,} companies, {counts['documents with text']:,} documents with text"
          + (f"; stopped by the {a.budget:g}-minute budget" if cut else "") + ".", "",
          "| Relation | Pairs | Companies | What it means |", "| --- | --: | --: | --- |",
          f"| same | {len(by('same')):,} | {companies_with('same'):,} | The same report twice (90%+ of each one's pages in the other): "
          f"{extra:,} copies that could be marked \"Duplicate of another document\" |",
          f"| contained | {len(by('contained')):,} | {companies_with('contained'):,} | A section or excerpt inside a larger document (80%+ of its pages) |",
          f"| overlap | {len(by('overlap')):,} | {companies_with('overlap'):,} | 30% to 80% of the smaller one's pages shared: a re-issue, an annex, a translation with tables in common |",
          "", "Examples (same report twice; A is the copy to keep: it has a web address, more pages or a type):", ""]
    for r in by("same")[:15]:
        md.append(f"- {r['company']}: A {r['a_label'] or r['a_library_id']} ({r['a_pages']} pages) / "
                  f"B {r['b_label'] or r['b_library_id']} ({r['b_pages']} pages), {r['shared_pages']} pages shared")
    md += ["", "The full list is near_duplicates.csv in the run's artifact. Nothing was changed."]
    open("near_duplicates.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    run.finish("partial" if cut else "ok", rows_in=counts["documents with text"], rows_out=len(rows), counters=dict(counts))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("near_duplicates.md", "w").write("### Near-duplicate documents: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
