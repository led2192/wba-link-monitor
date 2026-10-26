#!/usr/bin/env python3
"""
gold_eval.py: does the evidence database find the page the analyst cited?

The 2025 benchmarks left a validated answer per company and attribute (the "golden dataset": bases of
Attributes, each row with the value, the analyst's quote, the page and the source document). This
script takes the pilot attributes of gold_attributes.json and, for every validated answer with a quote
and a source, measures the evidence layer without any model:
  1. source found     the cited document is in the Disclosure Library (same company, same file size or
                      the same specific file name, as sl_labels.py matches) and its text is in the database;
  2. quote found      the first words of the analyst's quote occur in that document's text (a phrase
                      search inside the document); this also gives the page in our numbering, which can
                      differ from the printed page the analyst wrote;
  3. page ranked      with the attribute's search words, the quoted page is among the document's top 1,
                      3 or 10 pages (the best of the alternatives in gold_attributes.json counts).
Reads the gold bases with GOLD_TOKEN (read only), the Disclosure Library with AIRTABLE_TOKEN (read only)
and the evidence database with SUPABASE_DB_URL (read only). Writes gold_eval.md and the run's counters
to Pipeline QA. Never writes to the gold bases, the library or the evidence database.

Why this first: extraction reads the best passages, so if the cited page is not among them nothing
downstream can be right; and a quote that is nowhere in our text points at a text problem (a scan,
a different file, a garbled extraction) rather than a search problem.

Dry run by default: a sample of --limit answers per attribute (20 unless given), results printed;
--commit records the run in Pipeline QA (QA_COMMIT). Env: GOLD_TOKEN, AIRTABLE_TOKEN, AIRTABLE_BASE,
SUPABASE_DB_URL; AIRTABLE_QA_TOKEN (optional).
"""
import argparse, collections, json, os, re, statistics, sys, time, traceback
from urllib.parse import quote

import qa
from import_source_library import get_all
from sl_labels import match, specific_name

API = "https://api.airtable.com/v0"
CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gold_attributes.json")
QUOTE_WORDS = 6                 # words of the quote used as the phrase that locates it
TOP = 10
STOP = {"the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "by", "at", "as", "is", "are", "was", "were",
        "with", "from", "that", "this", "our", "its", "we", "it", "be", "has", "have", "had"}


# ----------------------------------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------------------------------
def gold_id(metadata_id):
    """'PT_01897-GMC_00064.1-2024' -> ('PT_01897', 'GMC_00064.1', 2024)."""
    m = re.match(r"^(PT_\d{5})-(GMC_\d{5}\.\d+)-(\d{4})$", (metadata_id or "").strip())
    return (m.group(1), m.group(2), int(m.group(3))) if m else (None, None, None)


def gold_pages(text):
    """The page numbers an analyst wrote: '63', 'p. 63', '63-64', '63, 70', 'pp. 12 and 14' -> [63, 64]."""
    out = []
    for a, b in re.findall(r"(\d{1,4})(?:\s*[-–]\s*(\d{1,4}))?", text or ""):
        a = int(a)
        if b and 0 < int(b) - a <= 3:
            out += list(range(a, int(b) + 1))
        else:
            out.append(a)
    return [p for p in dict.fromkeys(out) if 0 < p < 5000]


def quote_phrases(quote, n=QUOTE_WORDS):
    """Up to three phrases of n words from the analyst's quote, each a run of words the index can
    match: taken from the start of the quote and from the starts of its later sentences."""
    text = re.sub(r"\s+", " ", (quote or "")).strip()
    sentences = [s for s in re.split(r"(?<=[.;:])\s+|\n+", text) if s]
    phrases = []
    for s in [text] + sentences[1:]:
        words = [w for w in re.findall(r"[^\W_]+(?:[-'’][^\W_]+)*", s) if not w.isdigit() or len(w) > 2]
        if len(words) >= n:
            ph = " ".join(words[:n])
            if ph.lower() not in {p.lower() for p in phrases}:
                phrases.append(ph)
        if len(phrases) == 3:
            break
    return phrases


def or_query(q):
    """'"scope 1" emissions tCO2e' -> '"scope 1" or emissions or tCO2e': a page scores by how many of the
    terms it holds (and how often), instead of needing every one of them."""
    parts = re.findall(r'"[^"]+"|\S+', q or "")
    parts = [p for p in parts if p.lower() not in ("or", "and")]
    return " or ".join(parts)


def locate(found_by_phrase, analyst_pages, loose=5):
    """The pages to look for in the ranking: the pages of the most specific quote phrase when it is
    specific enough (at most `loose` pages; a phrase from a running header matches every page), else the
    analyst's pages, allowing two pages of offset between printed and PDF numbering."""
    sets = [set(v) for v in found_by_phrase if v]
    if sets:
        best = min(sets, key=len)
        if len(best) <= loose:
            return best
        near = {p + d for p in analyst_pages for d in range(-2, 3)}
        if near & best:
            return near & best
    return {p + d for p in analyst_pages for d in range(-2, 3)} if analyst_pages else set()


def rank_of(pages, ranking):
    """Best (lowest) 1-based rank of any of `pages` in `ranking` (a list of page numbers, best first), or None."""
    best = None
    for i, p in enumerate(ranking, 1):
        if p in pages and (best is None or i < best):
            best = i
    return best


def summarize(results):
    """Per-attribute counts from result dicts: {attribute, source_found, in_db, quote_found, best_rank}."""
    out = {}
    for r in results:
        s = out.setdefault(r["attribute"], collections.Counter())
        s["answers"] += 1
        if r.get("source_found"):
            s["source found"] += 1
        if r.get("in_db"):
            s["text in database"] += 1
        if r.get("quote_found"):
            s["quote found"] += 1
        rk = r.get("best_rank")
        if rk:
            s["ranks"] = s.get("ranks", 0)
            for k in (1, 3, 10):
                if rk <= k:
                    s[f"top {k}"] += 1
    return out


# ----------------------------------------------------------------------------------------------
# Airtable (read only)
# ----------------------------------------------------------------------------------------------
def gold_answers(token, base, codes):
    """Validated answers with a quote and a source, from one gold base: [{wba, attribute, year, quote, pages, source}]."""
    formula = "AND({Data Reviewed}, {Evidence}!='', {Source Link}!='', OR(" + \
              ",".join(f"FIND('{c}', {{Metadata ID}})" for c in codes) + "))"
    rows = get_all(token, base, "Metadata", ["Metadata ID", "Evidence", "Page Number", "Source Link", "Selected Value"], formula)
    src_ids = {s for r in rows for s in (r["fields"].get("Source Link") or [])}
    sources = {}
    srows = get_all(token, base, "Sources", ["Source ID", "WBA ID", "Source File", "Source Link", "Source Year"])
    for s in srows:
        if s["id"] in src_ids:
            f = s["fields"]
            att = (f.get("Source File") or [{}])[0]
            sources[s["id"]] = {"wba": (f.get("WBA ID") or "").strip(), "size": att.get("size"),
                                "filename": att.get("filename") or "", "url": f.get("Source Link") or "",
                                "name": f.get("Source ID") or ""}
    out = []
    for r in rows:
        f = r["fields"]
        wba, code, year = gold_id(f.get("Metadata ID"))
        if not code or code not in codes:
            continue
        src = [sources[s] for s in (f.get("Source Link") or []) if s in sources]
        out.append({"id": f.get("Metadata ID"), "wba": wba, "attribute": code, "year": year, "quote": f.get("Evidence") or "",
                    "pages": gold_pages(f.get("Page Number")), "value": f.get("Selected Value"), "sources": src})
    return out


def library_rows(token, base, wbas):
    """report_library rows (not discarded, with a file) of the given companies, in sl_labels.match shape."""
    H = {"Authorization": f"Bearer {token}"}
    out, wbas = [], sorted(set(w for w in wbas if w))
    for i in range(0, len(wbas), 40):
        formula = "AND(NOT({discard}), {file}!='', OR(" + ",".join(f"{{wba_id}}='{w}'" for w in wbas[i:i + 40]) + "))"
        params = [("pageSize", "100"), ("filterByFormula", formula)] + [("fields[]", x) for x in ("library_id", "wba_id", "file")]
        offset = None
        while True:
            p = params + ([("offset", offset)] if offset else [])
            from monitor_core import airtable_request
            j = airtable_request("GET", f"{API}/{base}/report_library", H, params=p).json()
            for r in j.get("records", []):
                out.append({"_id": r["id"], "library_id": r["fields"].get("library_id"), "wba_id": r["fields"].get("wba_id"),
                            "file": r["fields"].get("file")})
            offset = j.get("offset")
            if not offset:
                break
            time.sleep(0.2)
    return out


# ----------------------------------------------------------------------------------------------
# Evidence database (read only)
# ----------------------------------------------------------------------------------------------
def doc_in_db(cur, lid):
    cur.execute("select count(*) from public.pages where library_id = %s", (lid,))
    return cur.fetchone()[0]


def pages_with_phrase(cur, lid, phrase):
    cur.execute("select page from public.pages where library_id = %s and words @@ websearch_to_tsquery('simple', %s) "
                "order by page", (lid, f'"{phrase}"'))
    return [r[0] for r in cur.fetchall()]


def ranking(cur, lid, query, top=TOP):
    cur.execute("select page from public.pages, websearch_to_tsquery('simple', %s) q "
                "where library_id = %s and words @@ q order by ts_rank_cd(words, q) desc, page limit %s", (query, lid, top))
    return [r[0] for r in cur.fetchall()]


# ----------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="answers per attribute (dry run default 20; 0 = all)")
    ap.add_argument("--attributes", default="", help="comma-separated GMC codes (default: all in gold_attributes.json)")
    a = ap.parse_args()
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    limit = a.limit or (0 if a.commit else 20)
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    codes = [c for c in cfg["attributes"] if not a.attributes or c in a.attributes.split(",")]
    gold_token = os.environ.get("GOLD_TOKEN", "").strip()
    if not gold_token:
        open("gold_eval.md", "w").write("### Gold evaluation: GOLD_TOKEN is not set (a read-only token for the gold bases); nothing run.\n")
        print("GOLD_TOKEN not set", flush=True)
        return 0
    run = qa.Run("gold_eval.py")
    import psycopg

    answers = []
    for name, base in cfg["bases"].items():
        got = gold_answers(gold_token, base, codes)
        for g in got:
            g["base"] = name
        answers += got
        print(f">>> {name}: {len(got):,} validated answers with a quote and a source for {len(codes)} attributes", flush=True)
    by_attr = collections.defaultdict(list)
    for g in answers:
        by_attr[g["attribute"]].append(g)
    if limit:
        answers = [g for c in codes for g in by_attr[c][:limit]]

    rows = library_rows(os.environ["AIRTABLE_TOKEN"], os.environ["AIRTABLE_BASE"], [g["wba"] for g in answers])
    print(f">>> {len(rows):,} library rows of {len({g['wba'] for g in answers}):,} companies", flush=True)
    results, misses = [], collections.defaultdict(list)
    with psycopg.connect(os.environ["SUPABASE_DB_URL"].strip(), connect_timeout=30) as conn:
        cur = conn.cursor()
        cur.execute("set statement_timeout = '60s'")
        for g in answers:
            r = {"attribute": g["attribute"], "id": g["id"]}
            pairs = match(g["sources"], rows)                      # same company and file
            lid = pairs[0][1]["library_id"] if pairs else None
            r["source_found"] = bool(lid)
            if not lid:
                misses[g["attribute"]].append(f"{g['id']}: source not in the library ({(g['sources'] or [{}])[0].get('name', '?')})")
                results.append(r); continue
            r["in_db"] = doc_in_db(cur, lid) > 0
            if not r["in_db"]:
                misses[g["attribute"]].append(f"{g['id']}: no text in the database for {lid}")
                results.append(r); continue
            found = [pages_with_phrase(cur, lid, ph) for ph in quote_phrases(g["quote"])]
            r["quote_found"] = any(found)
            target = locate(found, g["pages"])
            if not r["quote_found"]:
                misses[g["attribute"]].append(f"{g['id']}: quote not found in {lid} (analyst page {g['pages']})")
            if target:
                best = None
                for qtext in cfg["attributes"][g["attribute"]]["queries"]:
                    rk = rank_of(target, ranking(cur, lid, or_query(qtext)))
                    if rk and (best is None or rk < best):
                        best = rk
                r["best_rank"] = best
                if best is None:
                    misses[g["attribute"]].append(f"{g['id']}: page {sorted(target)[:3]} not in the top {TOP} of any query ({lid})")
            results.append(r)

    summary = summarize(results)
    md = ["### Gold evaluation: does the search find the page the analyst cited?" + ("" if a.commit else " (dry run, sample)"), "",
          f"Validated answers with a quote and a source: {len(answers):,} across {len(codes)} attributes"
          + (f" ({limit} per attribute)" if limit else "") + ".", "",
          "| Attribute | Answers | Source in library | Text in DB | Quote found | Page in top 1 | top 3 | top 10 |",
          "| --- | --: | --: | --: | --: | --: | --: | --: |"]
    for c in codes:
        s = summary.get(c, collections.Counter())
        n = s["answers"] or 1
        pct = lambda k: f"{s[k]:,} ({s[k] / n:.0%})"
        md.append(f"| {c} {cfg['attributes'][c]['name']} | {s['answers']:,} | {pct('source found')} | {pct('text in database')} | "
                  f"{pct('quote found')} | {pct('top 1')} | {pct('top 3')} | {pct('top 10')} |")
    md.append("")
    for c in codes:
        if misses[c]:
            md += [f"Misses, {c}:", ""] + [f"- {m}" for m in misses[c][:15]] + [""]
    open("gold_eval.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    run.finish("ok", rows_in=len(answers), rows_out=len(results),
               counters={c: {k: v for k, v in summary.get(c, {}).items()} for c in codes})
    run.write_step_summary("ok", {c: summary.get(c, {}).get("top 10", 0) for c in codes}, title="Gold evaluation")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("gold_eval.md", "w").write("### Gold evaluation: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
