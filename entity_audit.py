#!/usr/bin/env python3
"""
entity_audit.py: library documents whose first pages never name the company they are filed under.

3 Nov 2026: in a hand review of 100 reader mistakes, 6 came from a document of another entity filed under the
company: JSW Infrastructure's and JSW Holdings' reports under JSW Steel, COFCO International's under COFCO,
ÖBB-Infrastruktur's under the ÖBB group, the State of Minnesota's under MN. A company's own report names the
company on its cover or the next pages; a subsidiary's, a sibling's or a stranger's names someone else.

For every company, in the evidence database (read-only): its documents with text whose pages 1 to 3 hold none
of the company's names (the WBA name, the legal name from data/sdg2000_2025.csv, and the name without its
legal words). Those are suspects, not verdicts: a cover can be a logo and a slogan, and a name can be printed
differently (an abbreviation, another script). The audit writes nothing: entity_audit.csv (one row per
suspect, with the first words of page 1) and entity_audit.md (the companies with most suspects), for a person
or a later rule to judge. Env: SUPABASE_DB_URL. Options: --limit N companies (0 = all), --wba ids.
"""
import argparse, collections, csv, os, re, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
LEGAL = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "ag", "se", "sa", "s",
         "a", "nv", "bv", "spa", "ab", "asa", "as", "oyj", "kgaa", "gmbh", "llc", "lp", "holdings", "holding", "group",
         "the", "and", "of", "de", "tbk", "pt", "bhd", "berhad", "pcl", "public", "jsc", "pjsc", "oao", "ooo", "kk", "k"}
FIRST_PAGES = 3
MIN_YEAR = 2020        # older reports carry former names (EADS for Airbus, Barclays Africa for Absa, Akzo for AkzoNobel)
OTHER_ENTITY = re.compile(r"((?:[A-Z][\w&'.-]*\s+){1,5}(?:B\.V\.|N\.V\.|Limited|Ltd\.?|Inc\.?|AG|SE|S\.A\.|SA|GmbH|plc|PLC|LLC|"
                          r"Corporation|Co\.,? Ltd\.?|S\.p\.A\.|A/S|AB|ASA|Oyj|Tbk|Berhad|Bhd))(?![\w])")


KEY_TYPES = ("Sustainability Report", "Annual Report", "Integrated Report", "ESG Report", "Climate Report", "CSR Report",
             "Registration Document", "10K Form", "Impact Report")   # the reports the reader reads (first run, 3 Nov 2026:
# 12.5% of all documents were suspects, most of them presentations, policies and untyped files that never print a name)


def names_of(company, corporate, website=""):
    """The company's names as regular expressions on raw page text: each name's words in order, any
    punctuation or spaces between them, whole words. Names under four letters match capitals only. Besides
    the whole names and the names without legal words: the first two words of each ("Air Products" for Air
    Products and Chemicals) and the website's name ("airproducts", "google" for Alphabet is not caught)."""
    out = set()
    for n in {company or "", corporate or ""}:
        words = re.findall(r"[^\W_]+", n)
        core = [w for w in words if w.lower() not in LEGAL] or words
        for ws in {tuple(words), tuple(core), tuple(core[:2])}:
            if ws and sum(len(w) for w in ws) >= 2:
                out.add(ws)
    m = re.match(r"(?:https?://)?(?:www\.)?([a-z0-9-]+)\.", (website or "").strip().lower())
    if m and len(m.group(1)) >= 4:
        out.add((m.group(1),))
    return sorted(out, key=lambda ws: -sum(map(len, ws)))


def pattern(ws):
    return r"\m" + r"[\W_]*".join(re.escape(w) for w in ws) + r"\M"


def suspects(cur, wba, company, corporate, website=""):
    """[(library_id, doc_type, year, label, url, cover)] of the company's documents with text whose first pages
    hold none of its names."""
    ns = names_of(company, corporate, website)
    if not ns:
        return []
    ci = [pattern(ws) for ws in ns if sum(map(len, ws)) >= 4]
    cs = [pattern(ws) for ws in ns if sum(map(len, ws)) < 4]
    conds, params = [], []
    if ci:
        conds.append("p.body ~* %s"); params.append("|".join(ci))
    if cs:
        conds.append("p.body ~ %s"); params.append("|".join(cs))
    cur.execute(
        "select d.library_id, coalesce(d.doc_type, ''), coalesce(d.year, 0), coalesce(d.label, ''), coalesce(d.url, ''), "
        "left(coalesce(p1.body, ''), 300) from public.documents d "
        "left join public.pages p1 on p1.library_id = d.library_id and p1.page = 1 "
        f"where d.wba_id = %s and d.text_status = 'ok' and d.doc_type = any(%s) and coalesce(d.year, 0) >= {MIN_YEAR} "
        "and length(coalesce(p1.body, '')) >= 40 "
        f"and not exists (select 1 from public.pages p where p.library_id = d.library_id and p.page <= {FIRST_PAGES} "
        f"and ({' or '.join(conds)}))", [wba, list(KEY_TYPES)] + params)
    return cur.fetchall()


def master():
    out = {}
    with open(os.path.join(HERE, "data", "sdg2000_2025.csv"), encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            w = (r.get("WBA ID") or "").strip()
            if w:
                out[w] = {"corporate": (r.get("Corporate Name") or "").strip(), "name": (r.get("WBA Company Name") or "").strip(),
                          "website": (r.get("Website") or "").strip()}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="companies (0 = all)")
    ap.add_argument("--wba", default="", help="wba ids, comma-separated")
    ap.add_argument("--budget", type=float, default=150, help="minutes")
    a = ap.parse_args()
    import psycopg
    m = master()
    rows, per, t0 = [], {}, time.time()
    with __import__("evidence_db").connect() as conn:
        cur = conn.cursor(); cur.execute("set statement_timeout = '120s'")
        cur.execute("select wba_id, max(company), count(*) from public.documents where text_status = 'ok' and wba_id <> '' "
                    "and doc_type = any(%s) group by wba_id order by wba_id", (list(KEY_TYPES),))
        comps = cur.fetchall()
        if a.wba:
            keep = set(a.wba.split(",")); comps = [c for c in comps if c[0] in keep]
        if a.limit:
            comps = comps[:a.limit]
        print(f">>> {len(comps):,} companies", flush=True)
        for i, (wba, company, n) in enumerate(comps, 1):
            if time.time() - t0 > a.budget * 60:
                print(f">>> stopped at the time budget after {i - 1} companies", flush=True); break
            info = m.get(wba, {})
            try:
                got = suspects(cur, wba, company or info.get("name"), info.get("corporate"), info.get("website", ""))
            except Exception as e:
                conn.rollback(); cur.execute("set statement_timeout = '120s'")
                print(f"    {wba}: {type(e).__name__}: {str(e)[:100]}", flush=True); continue
            per[wba] = (company, n, len(got))
            for lid, dt, y, label, url, cover in got:
                cover = re.sub(r"\s+", " ", cover)[:300]
                other = next((m.group(1).strip() for m in OTHER_ENTITY.finditer(cover)), "")
                rows.append([wba, company, lid, dt, y, label, url, cover, other])
            if i % 200 == 0:
                print(f"  {i:,}/{len(comps):,} companies, {len(rows):,} suspects", flush=True)
    with open("entity_audit.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh); w.writerow(["wba_id", "company", "library_id", "doc_type", "year", "label", "url", "page 1 begins", "names another entity"]); w.writerows(rows)
    docs = sum(v[1] for v in per.values()); sus = sum(v[2] for v in per.values())
    by_type = collections.Counter(r[3] or "(untyped)" for r in rows)
    strong = [r for r in rows if r[8]]
    md = ["### Entity audit: documents whose first pages never name the company (suspects, not verdicts)", "",
          f"{len(per):,} companies, {docs:,} reports with text (the types the reader reads); {sus:,} suspects ({sus / (docs or 1):.1%}).", "",
          "By type: " + ", ".join(f"{t} {n:,}" for t, n in by_type.most_common(12)) + ".", "",
          f"Of them, {len(strong):,} name another legal entity on page 1 (the strong suspects; the others name no one, "
          "often a logo or a slogan):", ""] + [f"- {r[1]} | {r[3]} {r[4]} | {r[5][:60]} | names \"{r[8]}\"" for r in strong[:60]] + ["",
          "| Company | Documents | Suspects | Share |", "| --- | --: | --: | --: |"]
    for wba, (co, n, k) in sorted(per.items(), key=lambda kv: (-kv[1][2] / max(kv[1][1], 1), -kv[1][2]))[:40]:
        if k:
            md.append(f"| {wba} {co} | {n} | {k} | {k / n:.0%} |")
    md += ["", "Examples (one in every few suspects, spread over the companies), to judge the audit itself:", ""]
    step = max(1, len(rows) // 60)
    for r in rows[::step][:60]:
        md.append(f"- {r[1]} | {r[3]} {r[4]} | {r[5][:70]} | page 1: \"{r[7][:110]}\"")
    md += ["", "Every suspect is in entity_audit.csv (the run's artifact) with the first words of page 1."]
    text = "\n".join(md) + "\n"
    open("entity_audit.md", "w").write(text); print(text, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
