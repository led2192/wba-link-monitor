#!/usr/bin/env python3
"""
year_window_eval.py: which documents' years does each attribute need? Learned from where the analysts' answers
come from, not set by hand.

Both readers (extract_eval.year_documents, narrative_eval.company_pages) read a company's documents of the
reporting year's document year (document_year: the year after it when the fiscal year ends January to May)
and the year after it: the window {0, +1}. A revenue figure must come from the year's own report, but a human
rights policy published the year before still answers the question (the owner, 5 Nov 2026). This script
measures, per attribute, where the documents the analysts cited sit against that window:

  for every validated answer (Data Reviewed) in the gold bases with a Source Link, each cited source is
  matched by address to the company's documents in the evidence database; its offset is that document's
  year minus the answer's document year. Sources not in our library are counted apart, with the offset of
  the analysts' own Source Year.

Per attribute: answers, share citing two or more sources, share of cited sources in our library, share of
cited documents inside today's window, the offsets, and the learned window: the narrowest run of offsets
around 0 that holds at least COVER of the cited documents (offsets -3 to +2). An attribute whose learned
window is inside {0, +1} is "period" (the year's own documents); one reaching back is "standing". With
--save it writes the windows to year_windows.json (attribute: window, kind, counts) for a reader option to
use later; nothing changes in any reader until that option is measured next to today's (CLAUDE.md).

Counts and shares only in the report (it is posted on a public repository): no company, no answer, no
address. Read-only: gold bases (GOLD_TOKEN, the analysts' bases), evidence database (SUPABASE_DB_URL).
Options: --bases (Airtable base ids, default the gold bases of gold_attributes.json), --min N (attributes
with fewer cited documents are listed apart), --save FILE, --budget minutes.
"""
import argparse, collections, json, os, re, sys, time, traceback
from urllib.parse import urlsplit

import qa
from gold_eval import gold_id

HERE = os.path.dirname(os.path.abspath(__file__))
COVER = 0.95                      # a learned window holds at least this share of the cited documents
OFFSETS = range(-3, 3)            # offsets a window may span
TODAY = (0, 1)                    # today's window in both readers
MIN_CITED = 20                    # fewer cited documents than this: listed, no window learned
YEAR = re.compile(r"(19|20)\d{2}")


def norm_url(u):
    """An address reduced to what identifies the file: no scheme, no www., no fragment, no trailing slash,
    lower case. 'https://www.X.com/a/b.pdf?x=1#p3' -> 'x.com/a/b.pdf?x=1'."""
    u = (u or "").strip()
    if not u:
        return ""
    s = urlsplit(u if "://" in u else "http://" + u)
    host = s.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    path = s.path.rstrip("/")
    return (host + path + (("?" + s.query) if s.query else "")).lower()


def file_key(u):
    """The file name of a PDF address, lower case, when it is distinctive enough to match on its own within one
    company: 'https://x.com/a/AR_2024_EN.pdf?v=2' -> 'ar_2024_en.pdf'. '' otherwise. 6 Nov 2026: matching the
    full address found 2% of the analysts' cited sources in our library; the same file is often served from
    another folder or host (a CDN, a localised site)."""
    path = urlsplit((u or "").strip() if "://" in (u or "") else "http://" + (u or "").strip()).path
    name = path.rstrip("/").rsplit("/", 1)[-1].lower()
    return name if name.endswith(".pdf") and len(name) >= 10 else ""


def first_year(v):
    m = YEAR.search(str(v or ""))
    return int(m.group(0)) if m else None


def learn_window(offsets, cover=COVER):
    """The narrowest (lo, hi) with lo <= 0 <= hi, within OFFSETS, holding at least `cover` of the offsets; on a
    tie the one reaching less far back. None when no window in OFFSETS reaches the cover. offsets: Counter."""
    n = sum(offsets.values())
    if not n:
        return None
    best = None
    for lo in range(0, OFFSETS.start - 1, -1):
        for hi in range(0, OFFSETS.stop):
            share = sum(v for k, v in offsets.items() if lo <= k <= hi) / n
            if share >= cover:
                key = (hi - lo, -lo, hi)
                if best is None or key < best[0]:
                    best = (key, (lo, hi), share)
                break
    return (best[1], best[2]) if best else None


def kind_of(window):
    if not window:
        return "unclear"
    lo, hi = window
    return "period" if lo >= TODAY[0] and hi <= TODAY[1] else "standing"


def tally(answers, sources, docs_by_company, doc_year):
    """Per attribute Counters: answers, multi, cited, matched, in_today, offsets (ours) and src_offsets
    (the analysts' Source Year, for sources not in our library). answers: [{wba, attribute, year, source_ids}];
    sources: {record id: {url, year}}; docs_by_company: {wba: {norm url: our year}}; doc_year(wba, year)."""
    out = collections.defaultdict(lambda: {"c": collections.Counter(), "off": collections.Counter(),
                                           "src": collections.Counter(), "sy": collections.Counter()})
    for g in answers:
        t = out[g["attribute"]]
        t["c"]["answers"] += 1
        ids = [i for i in g["source_ids"] if i in sources]
        if len(ids) >= 2:
            t["c"]["multi"] += 1
        want = doc_year(g["wba"], g["year"])
        mine = docs_by_company.get(g["wba"], {})
        for i in ids:
            t["c"]["cited"] += 1
            u = norm_url(sources[i]["url"])
            y = mine.get(u) if u else None
            if not y:
                k = file_key(sources[i]["url"])
                y = mine.get("file:" + k) if k else None
                t["c"]["matched by file name"] += bool(y)
            sy = first_year(sources[i]["year"])
            if sy:
                t["c"]["with a source year"] += 1
                t["sy"][sy - g["year"]] += 1
            if y:
                t["c"]["matched"] += 1
                off = y - want
                t["off"][off] += 1
                t["c"]["in_today"] += TODAY[0] <= off <= TODAY[1]
            elif sy:
                t["src"][sy - g["year"]] += 1
    return out


def pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "-"


def sy_cell(sy):
    """The analysts' own Source Year minus the reporting year, for every cited source with one (in our library
    or not): a second view that does not depend on our year labels."""
    n = sum(sy.values())
    if not n:
        return "-"
    part = lambda f: pct(sum(v for o, v in sy.items() if f(o)), n)
    return f"{part(lambda o: o <= -1)} / {part(lambda o: o == 0)} / {part(lambda o: o == 1)} / {part(lambda o: o >= 2)}"


def report(tallies, names, min_cited):
    md = ["### Which documents' years each attribute needs (learned from the analysts' citations)", "",
          "Validated answers with a cited source. Offset: the cited document's year in our library minus the answer's "
          "document year (the year after the reporting year when the fiscal year ends January to May). Today both readers "
          f"read offsets 0 and +1. Learned window: the narrowest run of offsets around 0 holding {COVER:.0%} of the cited "
          "documents found in our library. Source Year: the analysts' own year of each cited source minus the reporting year, "
          "for every source with one. Counts and shares only.", "",
          "| Attribute | Answers | Two or more sources | Cited in our library | Inside today's window | -3 or less | -2 | -1 | 0 | +1 | +2 or more | Learned window | Kind | Source Year -1 or less / 0 / +1 / +2 or more |",
          "| --- | --: | --: | --: | --: | --: | --: | --: | --: | --: | --: | --- | --- | --- |"]
    few, out = [], {}
    order = sorted(tallies, key=lambda c: -tallies[c]["c"]["matched"])
    for code in order:
        t = tallies[code]; c, off = t["c"], t["off"]
        m = c["matched"]
        if m < min_cited:
            few.append(f"{code} ({m})")
            continue
        w = learn_window(off)
        window, share = (w if w else (None, None))
        k = kind_of(window)
        out[code] = {"window": list(window) if window else None, "kind": k, "cover": round(share, 3) if share else None,
                     "cited_in_library": m, "inside_today": c["in_today"], "answers": c["answers"]}
        cell = lambda f: pct(sum(v for o, v in off.items() if f(o)), m)
        md.append(f"| {code} {names.get(code, '')[:45]} | {c['answers']:,} | {pct(c['multi'], c['answers'])} | {pct(m, c['cited'])} | "
                  f"{pct(c['in_today'], m)} | {cell(lambda o: o <= -3)} | {cell(lambda o: o == -2)} | {cell(lambda o: o == -1)} | "
                  f"{cell(lambda o: o == 0)} | {cell(lambda o: o == 1)} | {cell(lambda o: o >= 2)} | "
                  f"{('%+d to %+d' % tuple(window)) if window else 'none within -3 to +2'}"
                  f"{(' (' + pct(share, 1) + ')') if share else ''} | {k} | {sy_cell(t['sy'])} |")
    tot = collections.Counter()
    for t in tallies.values():
        tot.update(t["c"])
    md += ["", f"All attributes: {tot['answers']:,} answers, {tot['cited']:,} cited sources, {pct(tot['matched'], tot['cited'])} of them "
           f"found in our library, {pct(tot['in_today'], tot['matched'])} of those inside today's window; "
           f"{pct(tot['multi'], tot['answers'])} of the answers cite two or more sources."]
    period = sorted(c for c, v in out.items() if v["kind"] == "period")
    standing = sorted(c for c, v in out.items() if v["kind"] == "standing")
    md += ["", f"Period attributes ({len(period)}): {', '.join(period) or 'none'}.",
           f"Standing attributes ({len(standing)}): {', '.join(standing) or 'none'}."]
    src = collections.Counter()
    for t in tallies.values():
        src.update(t["src"])
    if src:
        n = sum(src.values())
        md += ["", f"Sources not in our library ({n:,}), by the analysts' Source Year minus the reporting year: "
               + ", ".join(f"{o:+d} {pct(v, n)}" for o, v in sorted(src.items()) if v / n >= 0.01) + "."]
    if few:
        md += ["", f"Fewer than {min_cited} cited documents in our library, no window learned: " + ", ".join(few) + "."]
    return md, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bases", default="", help="gold base ids, comma-separated (default: gold_attributes.json)")
    ap.add_argument("--min", type=int, default=MIN_CITED)
    ap.add_argument("--save", default="")
    ap.add_argument("--budget", type=float, default=40, help="minutes for the fiscal-year lookups")
    a = ap.parse_args()
    run = qa.Run("year_window_eval.py")
    token = os.environ["GOLD_TOKEN"].strip()
    cfg = json.load(open(os.path.join(HERE, "gold_attributes.json"), encoding="utf-8"))
    bases = [b for b in a.bases.split(",") if b] or list(cfg["bases"].values())
    names = {c: v.get("name", "") for c, v in json.load(open(os.path.join(HERE, "attributes.json"), encoding="utf-8"))["attributes"].items()}
    for c, v in (cfg.get("attributes") or {}).items():
        if isinstance(v, dict):
            names.setdefault(c, v.get("name", ""))
    from narrative_eval import read_base
    from value_eval import gold_sources
    answers, sources, seen = [], {}, set()
    for b in bases:
        rows = read_base(token, b, "Metadata", ["Metadata ID", "Source Link"], "AND({Data Reviewed}, {Source Link}&''!='')")
        src = gold_sources(token, b)
        sources.update(src)
        for r in rows:
            f = r["fields"]
            wba, code, year = gold_id(f.get("Metadata ID"))
            if not code or f.get("Metadata ID") in seen:
                continue
            seen.add(f.get("Metadata ID"))
            answers.append({"wba": wba, "attribute": code, "year": year, "source_ids": f.get("Source Link") or []})
        print(f">>> {b}: {len(rows):,} answers with a source, {len(src):,} sources", flush=True)
    companies = sorted({g["wba"] for g in answers})
    import extract_eval as E
    docs_by_company = collections.defaultdict(dict)
    t0, fy = time.time(), {}
    with __import__("evidence_db").connect() as conn:
        cur = conn.cursor()
        cur.execute("set statement_timeout = '60s'")
        for i in range(0, len(companies), 200):
            cur.execute("select wba_id, url, year from public.documents where wba_id = any(%s) and coalesce(url, '') <> '' and year > 0",
                        (companies[i:i + 200],))
            for wba, url, year in cur.fetchall():
                docs_by_company[wba].setdefault(norm_url(url), year)
                k = file_key(url)
                if k:
                    docs_by_company[wba].setdefault("file:" + k, year)
        print(f">>> {sum(len(v) for v in docs_by_company.values()):,} documents of {len(docs_by_company):,} companies", flush=True)
        for n, w in enumerate(companies, 1):
            if time.time() - t0 > a.budget * 60:
                print(f">>> fiscal-year lookups stopped at the budget after {n - 1:,} companies; the rest read as December", flush=True)
                break
            try:
                fy[w] = E.fy_end_month(cur, w)
            except Exception:
                conn.rollback(); cur.execute("set statement_timeout = '60s'")
                fy[w] = None
    doc_year = lambda w, y: E.document_year(y, fy.get(w))
    tallies = tally(answers, sources, docs_by_company, doc_year)
    md, windows = report(tallies, names, a.min)
    open("year_window_eval.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    if a.save:
        with open(a.save, "w", encoding="utf-8") as f:
            json.dump({"made_by": "year_window_eval.py --save", "made": time.strftime("%Y-%m-%d"), "cover": COVER,
                       "today": list(TODAY), "attributes": windows}, f, indent=1, sort_keys=True)
    run.finish("ok", rows_in=len(answers), rows_out=len(windows))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("year_window_eval.md", "w").write("### Year windows: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
