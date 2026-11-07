#!/usr/bin/env python3
"""
empty_pages.py: how many pages inside documents that do have text came out (nearly) empty?

Parser benchmarks shared by the owner (7 Nov 2026) name a silent failure: a page built as one image or one
degenerate form object gives no text and no error. Our covers are checked (cover_status no_text, ocr_covers.py),
pages inside a document are not. A page with no text is invisible to the page ranking and to every reader, so
a figure on it can only be found by OCR or by reading the page as an image (visual_eval.py).

On a random sample of documents in the evidence database (--sample, 1,500), counts pages whose text has fewer
than --min-chars letters and digits (80), split into: whole documents with no text, the first and last page
(often covers and back covers, expected), and pages in the middle of a document that has text (the silent
failure). Also by document type. Counts and shares only. Read-only.
"""
import argparse, collections, os, re, sys, traceback

SAMPLE = 1500
MIN_CHARS = 80


def classify(pages, min_chars=MIN_CHARS):
    """pages: [(page number, characters)] of one document. Returns a Counter of page kinds."""
    c = collections.Counter()
    if not pages:
        return c
    pages = sorted(pages)
    empty = [n < min_chars for _, n in pages]
    c["pages"] += len(pages)
    if all(empty):
        c["pages of documents with no text"] += len(pages)
        c["documents with no text"] += 1
        return c
    c["documents with text"] += 1
    first, last = pages[0][0], pages[-1][0]
    middle = 0
    for (p, _), e in zip(pages, empty):
        if not e:
            continue
        if p in (first, last):
            c["empty first or last page"] += 1
        else:
            middle += 1
    c["empty middle pages"] += middle
    c["documents with an empty middle page"] += bool(middle)
    c["documents with 10% or more of their middle pages empty"] += bool(len(pages) > 2 and middle / (len(pages) - 2) >= 0.10)
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=SAMPLE)
    ap.add_argument("--min-chars", type=int, default=MIN_CHARS)
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL", "").strip():
        open("empty_pages.md", "w").write("### Empty pages: SUPABASE_DB_URL is not set; nothing run.\n")
        return 0
    import evidence_db
    tot, by_type = collections.Counter(), collections.defaultdict(collections.Counter)
    with evidence_db.connect() as conn:
        cur = conn.cursor()
        cur.execute("set statement_timeout = '120s'")
        cur.execute("select library_id, coalesce(doc_type, '') from public.documents order by md5(library_id || 'empty-pages') limit %s", (a.sample,))
        docs = cur.fetchall()
        kinds = dict(docs)
        ids = [d[0] for d in docs]
        per = collections.defaultdict(list)
        for i in range(0, len(ids), 200):
            cur.execute("select library_id, page, length(regexp_replace(coalesce(body, ''), '[^[:alnum:]]', '', 'g')) "
                        "from public.pages where library_id = any(%s)", (ids[i:i + 200],))
            for lid, page, n in cur.fetchall():
                per[lid].append((page, n))
    for lid, pages in per.items():
        c = classify(pages, a.min_chars)
        tot.update(c); by_type[kinds.get(lid) or "(no type)"].update(c)
    pct = lambda x, n: f"{x / n:.1%}" if n else "-"
    md = ["### Pages without text inside documents that have text", "",
          f"A random sample of {len(ids):,} documents of the evidence database ({len(per):,} with pages); a page counts as empty "
          f"under {a.min_chars} letters and digits. Middle pages: neither the first nor the last page of a document that has text.", "",
          f"- Pages: {tot['pages']:,}; of documents with no text at all: {tot['pages of documents with no text']:,} ({tot['documents with no text']:,} documents)",
          f"- Documents with text: {tot['documents with text']:,}; with at least one empty middle page: {tot['documents with an empty middle page']:,} "
          f"({pct(tot['documents with an empty middle page'], tot['documents with text'])}); with 10% or more of their middle pages empty: "
          f"{tot['documents with 10% or more of their middle pages empty']:,} ({pct(tot['documents with 10% or more of their middle pages empty'], tot['documents with text'])})",
          f"- Empty middle pages: {tot['empty middle pages']:,} ({pct(tot['empty middle pages'], tot['pages'])} of all pages); empty first or last pages: {tot['empty first or last page']:,}", "",
          "| Document type | Documents with text | With an empty middle page | With 10%+ empty middle pages | Empty middle pages |",
          "| --- | --: | --: | --: | --: |"]
    for t, c in sorted(by_type.items(), key=lambda x: -x[1]["documents with text"]):
        if c["documents with text"] >= 10:
            md.append(f"| {t} | {c['documents with text']:,} | {pct(c['documents with an empty middle page'], c['documents with text'])} | "
                      f"{pct(c['documents with 10% or more of their middle pages empty'], c['documents with text'])} | {c['empty middle pages']:,} |")
    open("empty_pages.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("empty_pages.md", "w").write("### Empty pages: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
