#!/usr/bin/env python3
"""
one_hop.py: follow report-named links one level down and harvest the key reports found there.

The rule that came out of the link text capture experiment (anchor_capture_eval.py, 5 Nov 2026: on 150
companies, 400 PDFs of a key type labelled 2023 or later in 37 companies, one level below a link named like
a report; almost nothing direct). Today a report-named link to a landing page or microsite is opened by the
probe, found to be HTML and dropped, so the PDFs on that page are never seen.

Scope, kept narrow because the Disclosure Library is close to its record limit:
  companies  only those with a document gap (gap_pages: Sustainability report (recent), Annual report
             (recent), Code of Conduct, Human Rights Policy) or an overdue report (overdue_reports);
             --all for every company;
  pages      the report pages the plain monitor reads (reports_hub, sustainability_page, investor_relations;
             not the browser lane, not refused pages);
  links      named like a document (anchor_capture_eval.named), same company site, not held, not a .pdf
             address (the harvester takes those already); at most --per-page opened per page;
  keeps      a PDF on the landing page that is not held (by address, by file name, by doc_id, by purge
             tombstone), whose type (named by its own link text, else by the link that led there) is in
             the sustainability or annual report family with a label year of 2023 or later, or is a Code of
             Conduct or Human Rights Policy the company lacks; at most --per-company per company and
             --max-new per run.
The label year is the year the link text (else the address) names: it decides only "recent enough", never
the reporting year, which the pipeline reads from the document as for any other row.

Record guard: reads the latest base_health row and writes nothing that would take records_total past
GUARD_SHARE (85%) of RECORD_LIMIT; --max-new is cut to the room left.

New rows go through harvest_reports.upsert (deterministic library_id, doc_id guard, tombstones), linked to
the monitored page the chain started from; link_text is "<the link followed> > <the PDF's link>". Dry run by
default; --commit writes. Env: AIRTABLE_TOKEN, AIRTABLE_BASE; AIRTABLE_QA_TOKEN for the run record;
RECORD_LIMIT (500,000). Writes one_hop.md and one_hop.csv (every PDF kept).
"""
import argparse, collections, csv, hashlib, os, re, sys, threading, time, traceback
from concurrent.futures import ThreadPoolExecutor

import requests

import qa
from anchor_capture_eval import (KEY_TYPES, PAGE_TYPES, RECENT, Held, candidates, one_hop_pdfs,
                                 open_link, spread)
from anchor_eval import fetch, text_type, text_year
from company_coverage import ANNUAL_FAMILY, SUST_FAMILY

RECORD_LIMIT = int(os.environ.get("RECORD_LIMIT") or 500_000)
GUARD_SHARE = 0.85
DOC_GAPS = ("Sustainability report (recent)", "Annual report (recent)", "Code of Conduct", "Human Rights Policy")
POLICIES = {"Code of Conduct", "Human Rights Policy"}


def says_nothing(text):
    """True when a PDF's own link text names no document of its own: empty, generic ("Download", "PDF"), or
    only a report word ("Full report", "Read the report"). Then the link that led to the page names it.
    "Minutes of the AGM" says something else, so it does not inherit the annual report's name."""
    from anchor_capture_eval import REPORT_WORD
    from anchor_eval import GENERIC
    t = re.sub(r"\(.*?\)|\bpdf\b|\d+(\.\d+)?\s*[km]b\b", " ", (text or ""), flags=re.I).strip()
    if not t or GENERIC.match(t):
        return True
    words = re.findall(r"[^\W\d_]+", t.lower())
    rest = [w for w in words if not REPORT_WORD.fullmatch(w) and w not in
            {"full", "read", "the", "our", "view", "download", "here", "complete", "version", "english", "en"}]
    return not rest


DOC_NOUN = re.compile(r"report|review|accounts|statement|policy|policies|code|10-?k|20-?f|memoria|relat[oó]rio|"
                      r"informe|reporte|rapport|bericht|verslag|報告|レポート|보고서", re.I)
INHERIT_MAX = 3        # a landing page with more PDFs than this is an archive: a wordless link there names nothing


def address_year(url):
    """The year a PDF's address names: the file name's first, then the folders' (KIC_2022_en.pdf under
    /annual-report/2025/ is 2022). 0 when none."""
    from urllib.parse import unquote, urlsplit
    path = unquote(urlsplit(url).path)
    for part in (path.rsplit("/", 1)[-1], path):
        ys = [int(y) for y in re.findall(r"(?<!\d)(20[0-3]\d)(?!\d)", part)]
        if ys:
            return max(ys)
    return 0


# Documents named like a key report or policy that are not the company's own (first commit run, 5 Nov 2026,
# 174 rows): supplier codes counted as the Code of Conduct, bond allocation reports and PRI scorecards as impact
# reports, a bond-issuing subsidiary's annual report, environmental and social assessments of a lender's clients.
NOT_OWN = re.compile(r"supplier|vendor|contractor|business partner|third[- ]part|\bbonds?\b|allocation|\bMTN\b|"
                     r"prospectus|scorecard|\bPRI\b|impact assessment|\bESIA\b|\baudit\b|due diligence|"
                     r"notice of (the )?(annual )?(general )?meeting", re.I)


def keep(pdf_text, parent_text, pdf_url, lacking, may_inherit=True):
    """(type, label year) when a one-hop PDF is worth a library row, else None. Reports: sustainability or
    annual family, labelled RECENT or later. Policies: only one the company lacks, any year.
    The type comes from link text that names a document ("Open Data Plan" names "annual" but no document:
    5 Nov 2026 dry run, Hong Kong Monetary Authority). A PDF whose own text says nothing takes the type of
    the link that led to its page only on a small landing page (may_inherit). The year: the PDF's own text,
    then its address, then the link that led there (a 2025 report microsite also lists the 2023 and 2024
    reports: KIC_2023_en.pdf is 2023, whatever the parent link says)."""
    if NOT_OWN.search(pdf_text or "") or NOT_OWN.search(parent_text or ""):
        return None
    typ = text_type(pdf_text) if DOC_NOUN.search(pdf_text or "") else ""
    inherited = False
    if not typ and may_inherit and says_nothing(pdf_text) and DOC_NOUN.search(parent_text or ""):
        typ, inherited = text_type(parent_text), True
    if typ not in KEY_TYPES:
        return None
    year = int(text_year(pdf_text) or 0) or address_year(pdf_url) or int(text_year(parent_text) or 0)
    if typ in POLICIES:
        return (typ, year) if typ in lacking else None
    return (typ, year) if year >= RECENT else None


def lacking_of(gap_pages):
    """The policies a company lacks, from its gap_pages tags."""
    return {g for g in (gap_pages or []) if g in POLICIES}


def room(H, base):
    """Records that can still be added under the guard: GUARD_SHARE of RECORD_LIMIT minus the latest
    base_health records_total. None when there is no reading (then nothing is written)."""
    rows = fetch(H, base, "base_health", ["date", "records_total"], "{records_total} > 0")
    rows = sorted(rows, key=lambda r: r["fields"].get("date", ""), reverse=True)
    if not rows:
        return None, None
    total = int(rows[0]["fields"]["records_total"])
    return int(GUARD_SHARE * RECORD_LIMIT) - total, total


def company_finds(wba, pages, held, lacking, per_page, per_company, deadline,
                  session_factory=requests.Session, pause=1.0):
    """Library rows (not yet written) for one company, and counters."""
    s = session_factory()
    out, c = [], collections.Counter()
    for page in pages:
        if time.time() > deadline or len(out) >= per_company:
            break
        kind, html, final = open_link(s, page["url"])
        time.sleep(pause)
        if kind != "html":
            c["page not read"] += 1
            continue
        c["pages read"] += 1
        for u, text, _ in candidates(html, final or page["url"], held, per_page):
            if time.time() > deadline or len(out) >= per_company:
                break
            got, body, final2 = open_link(s, u)
            time.sleep(pause)
            c["named links opened"] += 1
            if got != "html":
                continue
            on_page = one_hop_pdfs(body, final2 or u, held)
            for pu, ptext, elsewhere in on_page:
                if elsewhere:
                    c["held under another address"] += 1
                    continue
                k = keep(ptext, text, pu, lacking, may_inherit=len(on_page) <= INHERIT_MAX)
                if not k:
                    c["not a key recent document"] += 1
                    continue
                held.add(pu)
                out.append({"wba_id": wba, "company_name": page.get("company", ""), "document_url": pu,
                            "found_on": page["url"], "page_type": page["type"], "via": final2 or u,
                            "link_text": f"{text} > {ptext}"[:200], "type_named": k[0], "label_year": k[1]})
                c["kept"] += 1
                if len(out) >= per_company:
                    break
    return out, c


def library_rows(finds):
    """The rows harvest_reports.upsert writes, with the same library_id as a harvested document."""
    from harvest_reports import year_of
    from ids import link_id, normalize
    rows = []
    for f in finds:
        key = normalize(f["document_url"]) or f["document_url"].strip().lower()
        lib_id = f"{(str(f['wba_id']).strip() or 'NA')}-{hashlib.sha1(key.encode()).hexdigest()[:10]}"
        rows.append({"library_id": lib_id, "wba_id": f["wba_id"], "company_name": f["company_name"],
                     "document_url": f["document_url"], "found_on": f["found_on"], "page_type": f["page_type"],
                     "doc_year": year_of(f["document_url"]), "source_link_id": link_id(f["wba_id"], f["found_on"]),
                     "link_text": f["link_text"]})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write the new rows (default: dry run)")
    ap.add_argument("--all", action="store_true", help="every company, not only those with a document gap")
    ap.add_argument("--companies", type=int, default=0, help="at most N companies, spread across the list (0 = all)")
    ap.add_argument("--per-page", type=int, default=12)
    ap.add_argument("--per-company", type=int, default=10)
    ap.add_argument("--max-new", type=int, default=300, help="new rows per run at most")
    ap.add_argument("--budget", type=float, default=100, help="minutes")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    base, token = os.environ["AIRTABLE_BASE"], os.environ["AIRTABLE_TOKEN"]
    H = {"Authorization": f"Bearer {token}"}
    run = qa.Run("one_hop.py")
    left, total = room(H, base)
    max_new = a.max_new if left is None else max(0, min(a.max_new, left))
    guard = (f"records_total {total:,} of {RECORD_LIMIT:,} ({total / RECORD_LIMIT:.0%}); guard at {GUARD_SHARE:.0%}: "
             f"room for {max(left, 0):,}" if total is not None else "no base_health reading")
    print(f">>> {guard}; at most {max_new:,} new rows this run", flush=True)
    if a.commit and (left is None or max_new == 0):
        md = ["### One hop: nothing written", "", f"Record guard: {guard}."]
        open("one_hop.md", "w").write("\n".join(md) + "\n"); print("\n".join(md))
        run.finish("skipped", notes=guard)
        return 0

    pages = fetch(H, base, "monitored_links", ["wba_id", "company_name", "url", "type"],
                  "AND({monitor}, NOT({needs_browser}), NOT({status}='blocked'), OR("
                  + ",".join(f"{{type}}='{t}'" for t in PAGE_TYPES) + "))")
    by = collections.defaultdict(list)
    for r in pages:
        f = r["fields"]
        if f.get("wba_id") and f.get("url"):
            by[f["wba_id"]].append({"url": f["url"], "type": f.get("type", ""), "company": f.get("company_name", ""),
                                    "id": r["id"]})
    gaps = {}
    for r in fetch(H, base, "companies", ["wba_id", "gap_pages", "overdue_reports"]):
        f = r["fields"]
        if f.get("wba_id"):
            gaps[f["wba_id"]] = (f.get("gap_pages") or [], f.get("overdue_reports") or [])
    target = [w for w in by if a.all or any(g in DOC_GAPS for g in gaps.get(w, ([], []))[0]) or gaps.get(w, ([], []))[1]]
    target = spread(target, a.companies)
    print(f">>> {len(target):,} companies ({'all' if a.all else 'with a document gap or an overdue report'}), "
          f"{sum(len(by[w]) for w in target):,} report pages", flush=True)

    from harvest_reports import existing_library, upsert
    existing, held = {}, collections.defaultdict(Held)
    for i in range(0, len(target), 25):
        ors = ",".join(f"{{wba_id}}='{w}'" for w in target[i:i + 25])
        part = existing_library(base, token, "report_library", f"OR({ors})")
        existing.update(part)
        for v in part.values():
            if v.get("url"):
                held[v["wba"]].add(v["url"])

    deadline = time.time() + a.budget * 60
    finds, counters, lock = [], collections.Counter(), threading.Lock()

    def one(wba):
        if len(finds) >= max_new:
            return
        try:
            f, c = company_finds(wba, by[wba], held[wba], lacking_of(gaps.get(wba, ([], []))[0]),
                                 a.per_page, a.per_company, deadline)
        except Exception as e:                 # 5 Nov 2026: one site's charset ended the whole run, nothing written
            print(f"  {wba}: {type(e).__name__}: {str(e)[:120]}", flush=True)
            f, c = [], collections.Counter({f"company failed ({type(e).__name__})": 1})
        with lock:
            finds.extend(f[:max(0, max_new - len(finds))]); counters.update(c)
            counters["companies done"] += 1
            if counters["companies done"] % 50 == 0:
                print(f"  {counters['companies done']:,}/{len(target):,} companies, {len(finds):,} kept", flush=True)

    with ThreadPoolExecutor(a.workers) as ex:
        list(ex.map(one, target))
    cut = time.time() > deadline

    with open("one_hop.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["wba_id", "company_name", "type_named", "label_year", "document_url",
                                           "via", "found_on", "page_type", "link_text"])
        w.writeheader(); w.writerows(finds)
    created, blocked = [], 0
    rows = library_rows(finds)
    if a.commit and rows:
        from ids import normalize
        id_by_url = {normalize(p["url"]): p["id"] for ps in by.values() for p in ps}
        created, _, blocked, _ = upsert(rows, id_by_url, base, token, "report_library", existing=existing)
    types = collections.Counter(f["type_named"] for f in finds)
    md = ["### One hop: key reports one level below a report-named link", "",
          f"Record guard: {guard}. {len(target):,} companies; {counters['pages read']:,} report pages read; "
          f"{counters['named links opened']:,} named links opened" + ("; stopped by the time budget" if cut else "") + ".", "",
          f"Kept: {len(finds):,} PDFs in {len({f['wba_id'] for f in finds}):,} companies "
          f"(cap {max_new:,} a run, {a.per_company} a company). Left out: {counters['held under another address']:,} held "
          f"under another address, {counters['not a key recent document']:,} not a key type or older than {RECENT}.", "",
          ("Written: " + f"{len(created or []):,} new rows; {blocked:,} skipped as known documents (doc_id guard or tombstone)."
           if a.commit else "Dry run: nothing written (--commit writes)."), "",
          "By type: " + ", ".join(f"{t} {n:,}" for t, n in types.most_common()), "", "Examples:", ""]
    md += [f"- {f['company_name'] or f['wba_id']}: {f['type_named']} {f['label_year'] or ''} -> {f['document_url'][:110]}"
           for f in finds[:20]]
    open("one_hop.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    run.finish("partial" if cut else "ok", rows_in=counters["named links opened"],
               rows_out=len(created or []) if a.commit else len(finds), counters=dict(counters))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        open("one_hop.md", "w").write("### One hop: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        qa.record_exception("one_hop.py", e)
        raise
