#!/usr/bin/env python3
"""
anchor_capture_eval.py: would the text of a link find documents the monitors miss today?

Proposal 6 of the information retrieval review (anchor text), applied to capture instead of labelling.
Today a link becomes a document candidate only by its address (monitor_core.doc_links: .pdf, report-ish
paths, document-server endpoints); its text is kept as annotation and never triggers a match. Two kinds of
report are lost that way:
  direct    a link named like a report ("Annual Report 2024", "Sustainability Report (PDF)") whose address
            says nothing (/en/node/4411, /handler?id=93) but serves the PDF;
  one hop   a link named like a report that leads to an HTML page (a report microsite, a landing page)
            with the PDF one level down. The probe opens such addresses, finds no PDF and drops them,
            and the landing page is not a monitored page, so its PDFs are never seen.

This script measures both before anything changes, on the report pages the plain monitor reads
(reports_hub, sustainability_page, investor_relations; not the browser lane, not refused pages):
  1. reads each page and keeps the links whose text names a document type or a report word;
  2. opens those not already held in the library and not already a .pdf address (at most --per-page per
     page, one request a second per company, its own budget);
  3. a PDF answer is a direct find; an HTML answer is read for its document links (doc_links), and the
     PDFs among them not held are one-hop finds.
A find is a PDF this company's library does not hold, by normalised address. The year a link text names
is reported as the document's label ("Annual Report 2025"), not as its reporting year: a report named
2025 often covers 2024, and the reporting year comes from the period the document covers.

Read-only against the Disclosure Library; reads company web pages the monitors already read, plus the
links on them. No model calls. Writes anchor_capture.csv (one row per link opened) and anchor_capture.md.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE; AIRTABLE_QA_TOKEN for the run record.
Options: --companies N (default 150, spread across the list), --per-page N (default 12), --budget minutes.
"""
import argparse, collections, csv, hashlib, os, re, sys, threading, time, traceback
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlsplit

import requests

import qa
from anchor_eval import GENERIC, fetch, text_type, text_year
from company_coverage import ANNUAL_FAMILY, SUST_FAMILY
from monitor_core import doc_links, is_framework_host, normalize, reg_domain

# What the completeness checks need: the sustainability and annual report families, the code of conduct and
# the human rights policy. A find of another type (statutory documents, presentations) is a document, but
# not one that closes a gap.
KEY_TYPES = SUST_FAMILY | ANNUAL_FAMILY | {"Code of Conduct", "Human Rights Policy"}
RECENT = 2023

PAGE_TYPES = ("reports_hub", "sustainability_page", "investor_relations")
REPORT_WORD = re.compile(r"\b(report|annual|sustainab\w*|integrated|esg|csr|impact|climate|tcfd|"
                         r"modern slavery|human rights|code of conduct|policy|statement|"
                         r"memoria|rapport|bericht|relat[oó]rio|informe|jaarverslag|verslag)\b", re.I)
NOT_DOC_EXT = re.compile(r"\.(png|jpe?g|gif|svg|webp|mp[34]|zip|xlsx?|docx?|pptx?|csv|xml|json)([?#]|$)", re.I)
UA = {"User-Agent": "Mozilla/5.0 (compatible; WBA-LinkMonitor/1.0)",
      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "Accept-Language": "en-US,en;q=0.9"}
HTML_MAX = 2_000_000


GENERIC_NAME = re.compile(r"^(report|download|document|file|index|annual[-_ ]?report|sustainability[-_ ]?report|pdf)\d{0,2}\.pdf$", re.I)


def file_key(url):
    """The file name of a PDF address, lower case and decoded ('ad_2024_esp.pdf'), or '' when it is too
    generic to identify a file. The same report is often served from the company's site and from a CDN
    under another path (Arcos Dorados, 4 Nov 2026: www.arcosdorados.com/wp-content/uploads/2025/07/AD_2024_ESP.pdf
    and ddhnb9vx8lz57.cloudfront.net/wp-content/uploads/2025/07/29174027/AD_2024_ESP.pdf), so an address
    match alone counts a held report as new."""
    from urllib.parse import unquote
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1]).strip().lower()
    if not name.endswith(".pdf") or len(name) < 10 or GENERIC_NAME.match(name):
        return ""
    return name


class Held:
    """The documents a company's library holds, by normalised address and by file name."""
    def __init__(self, urls=()):
        self.addresses, self.names = set(), set()
        for u in urls:
            self.add(u)

    def add(self, url):
        self.addresses.add(normalize(url))
        k = file_key(url)
        if k:
            self.names.add(k)

    def __contains__(self, url_or_key):
        return url_or_key in self.addresses or normalize(url_or_key) in self.addresses

    def by_name(self, url):
        k = file_key(url)
        return bool(k) and k in self.names


def named(text):
    """True when a link's text names a document: a document type, or a report word."""
    t = (text or "").strip()
    if not t or GENERIC.match(t):
        return False
    return bool(text_type(t) or REPORT_WORD.search(t))


def anchors(html, base):
    """[(absolute url, text)] for every <a href> of the page with text, first occurrence of each address."""
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin
    out, seen = [], set()
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        try:
            u = urljoin(base, href)
            n = normalize(u)
        except ValueError:
            continue
        if n and n not in seen:
            seen.add(n)
            out.append((u, a.get_text(" ", strip=True)[:120]))
    return out


def candidates(html, base, held, per_page):
    """The named links of a page worth opening: same company domain (or a non-framework host for a link
    the address rules already take), not held, not a .pdf address (those the harvester takes already),
    not an image or spreadsheet. Returns [(url, text, shaped)], shaped = today's address rules take it."""
    shaped = set(doc_links(html, base))
    rdom = reg_domain(base)
    out = []
    for u, text in anchors(html, base):
        if not named(text):
            continue
        n = normalize(u)
        s = urlsplit(u)
        path = (s.path or "").lower()
        if n in held or path.endswith(".pdf") or NOT_DOC_EXT.search(path) or is_framework_host(s.hostname):
            continue
        if reg_domain(u) != rdom and n not in shaped:
            continue
        if normalize(base) == n:
            continue
        out.append((u, text, n in shaped))
        if len(out) >= per_page:
            break
    return out


def decode(data, encoding):
    """Bytes to text with the page's declared encoding, falling back to UTF-8 when the declaration is not a
    codec Python knows (5 Nov 2026: a site sent charset=utf-8,gbk and the One hop run crashed)."""
    for enc in ((encoding or "").split(",")[0].strip() or "utf-8", "utf-8"):
        try:
            return data.decode(enc, "replace")
        except LookupError:
            continue
    return data.decode("utf-8", "replace")


def open_link(session, url, timeout=20):
    """('pdf', None) / ('html', text) / ('other', None) / ('err', None). Reads the first KB of a file and at
    most HTML_MAX characters of a page."""
    try:
        r = session.get(url, headers=UA, timeout=timeout, stream=True, allow_redirects=True)
    except requests.RequestException:
        return "err", None, url
    try:
        if r.status_code >= 400:
            return "err", None, r.url
        ctype = (r.headers.get("Content-Type") or "").lower()
        it = r.iter_content(65536)
        head = next(it, b"")
        if "pdf" in ctype or head.startswith(b"%PDF"):
            return "pdf", None, r.url
        if "html" not in ctype:
            return "other", None, r.url
        buf = [head]; size = len(head)
        for chunk in it:
            buf.append(chunk); size += len(chunk)
            if size >= HTML_MAX:
                break
        return "html", decode(b"".join(buf), r.encoding), r.url
    finally:
        r.close()


def one_hop_pdfs(html, base, held):
    """PDF-shaped document links of a landing page that the library does not hold at that address:
    [(url, text, held under another address with the same file name)]."""
    out = []
    for n, (u, text) in doc_links(html, base).items():
        if n in held:
            continue
        if urlsplit(u).path.lower().endswith(".pdf"):
            out.append((u, text, held.by_name(u)))
    return out


def spread(ids, n):
    """n wba ids spread across the list (by a hash of the id, so the same sample on every run)."""
    return sorted(ids, key=lambda w: hashlib.sha1(w.encode()).hexdigest())[:n] if n else sorted(ids)


def company_run(wba, pages, held, per_page, deadline, session_factory=requests.Session, pause=1.0):
    """Every opened link of one company's pages, as rows. One request at a time per company."""
    s = session_factory()
    rows, c = [], collections.Counter()
    for page in pages:
        if time.time() > deadline:
            c["stopped by budget"] += 1
            break
        kind, html, final = open_link(s, page["url"])
        time.sleep(pause)
        if kind != "html":
            c["page not read"] += 1
            continue
        c["pages read"] += 1
        base = final or page["url"]
        cands = candidates(html, base, held, per_page)
        c["named links opened"] += len(cands)
        for u, text, shaped in cands:
            if time.time() > deadline:
                break
            got, body, final2 = open_link(s, u)
            time.sleep(pause)
            row = {"wba_id": wba, "company": page.get("company", ""), "page": page["url"], "page_type": page["type"],
                   "link": u, "link_text": text, "address_rules_take_it": "yes" if shaped else "",
                   "answer": got, "find": "", "found_pdf": "", "found_text": "",
                   "type_named": text_type(text), "label_year": text_year(text)}
            if got == "pdf":
                pdf = final2 or u
                if pdf not in held:
                    row["find"] = "held elsewhere" if held.by_name(pdf) else "probe reach" if shaped else "direct"
                    row["found_pdf"] = pdf
                    held.add(pdf)
                rows.append(row)
            elif got == "html":
                found = one_hop_pdfs(body, final2 or u, held)
                if not found:
                    rows.append(row)
                for pu, ptext, elsewhere in found:
                    held.add(pu)
                    rows.append({**row, "find": "held elsewhere" if elsewhere else "one hop", "found_pdf": pu, "found_text": ptext,
                                 "type_named": text_type(ptext) or row["type_named"],
                                 "label_year": text_year(ptext) or row["label_year"]})
            else:
                rows.append(row)
    return rows, c


def fetch_inputs(H, base, companies):
    pages = fetch(H, base, "monitored_links", ["wba_id", "company_name", "url", "type", "status"],
                  "AND({monitor}, NOT({needs_browser}), NOT({status}='blocked'), OR("
                  + ",".join(f"{{type}}='{t}'" for t in PAGE_TYPES) + "))")
    by = collections.defaultdict(list)
    for r in pages:
        f = r["fields"]
        if f.get("wba_id") and f.get("url"):
            by[f["wba_id"]].append({"url": f["url"], "type": f.get("type", ""), "company": f.get("company_name", "")})
    chosen = spread(list(by), companies)
    held = collections.defaultdict(Held)
    for i in range(0, len(chosen), 25):
        ors = ",".join(f"{{wba_id}}='{w}'" for w in chosen[i:i + 25])
        for r in fetch(H, base, "report_library", ["wba_id", "document_url"], f"OR({ors})"):
            f = r["fields"]
            if f.get("document_url"):
                held[f["wba_id"]].add(f["document_url"])
    return {w: by[w] for w in chosen}, held


def find_year(r):
    """The year a find is labelled with: its link text's, else the latest year in its address ('' if none)."""
    if r.get("label_year"):
        return int(r["label_year"])
    ys = [int(y) for y in re.findall(r"(?<!\d)(20[0-3]\d)(?!\d)", r.get("found_pdf") or "")]
    return max(ys) if ys else 0


def summary(rows, counters, n_companies, cut):
    finds = [r for r in rows if r["find"]]
    new = [r for r in finds if r["find"] in ("direct", "one hop")]
    per_company = collections.Counter(r["company"] or r["wba_id"] for r in new)
    top3 = sum(n for _, n in per_company.most_common(3))
    recent = sum(1 for r in new if find_year(r) >= RECENT)
    key = [r for r in new if r["type_named"] in KEY_TYPES]
    key_recent = [r for r in key if find_year(r) >= RECENT]
    kind = collections.Counter(r["find"] for r in finds)
    comp = lambda k: len({r["wba_id"] for r in finds if r["find"] == k})
    types = collections.Counter(r["type_named"] or "(no type named)" for r in new)
    md = ["### Link text for capture: documents the monitors miss today", "",
          f"{n_companies:,} companies; {counters['pages read']:,} report pages read ({counters['page not read']:,} did not answer "
          f"with a page); {counters['named links opened']:,} links named like a document opened (not held, not a .pdf address)"
          + ("; stopped by the time budget" if cut else "") + ".", "",
          "| Find | PDFs not in the library | Companies | What it means |", "| --- | --: | --: | --- |",
          f"| direct | {kind['direct']:,} | {comp('direct'):,} | The link's address says nothing, its text names a document, and it serves the PDF: today's rules never open it |",
          f"| one hop | {kind['one hop']:,} | {comp('one hop'):,} | The named link leads to a landing page whose PDFs we do not hold: never seen today |",
          f"| probe reach | {kind['probe reach']:,} | {comp('probe reach'):,} | The address rules take the link and the nightly probe would find the PDF (already in reach; not new) |",
          f"| held elsewhere | {kind['held elsewhere']:,} | {comp('held elsewhere'):,} | A PDF with the same file name is held under another address (a CDN copy): not new |",
          "", f"Concentration: the three companies with most finds hold {top3:,} of the {len(new):,} direct and one-hop finds "
          f"({top3 / (len(new) or 1):.0%}): " + "; ".join(f"{c} {n:,}" for c, n in per_company.most_common(3)) + ". "
          f"Finds labelled {RECENT} or later: {recent:,}.", "",
          f"What could close a gap: {len(key):,} finds of a key type (sustainability or annual report family, code of conduct, "
          f"human rights policy) in {len({r['wba_id'] for r in key}):,} companies; {len(key_recent):,} of them labelled {RECENT} or later, "
          f"in {len({r['wba_id'] for r in key_recent}):,} companies. A nightly rule would keep only these.",
          "", "Type named by the link text, direct and one-hop finds:", ""]
    md += [f"- {t}: {n:,}" for t, n in types.most_common(12)]
    md += ["", "Examples (the year is the document's label as the company names it, not its reporting year):", ""]
    shown = collections.Counter()
    for r in key_recent + [r for r in new if r not in key_recent]:
        if shown[r["wba_id"]] >= 2 or sum(shown.values()) >= 20:
            continue
        shown[r["wba_id"]] += 1
        md.append(f"- {r['company'] or r['wba_id']}: \"{r['link_text'][:60]}\" ({r['find']}) -> {r['found_pdf'][:110]}")
    md += ["", "Every opened link is in anchor_capture.csv. Nothing was changed."]
    return md


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--companies", type=int, default=150, help="companies, spread across the list (0 = all)")
    ap.add_argument("--per-page", type=int, default=12, help="named links opened per page at most")
    ap.add_argument("--budget", type=float, default=100, help="minutes")
    ap.add_argument("--workers", type=int, default=8, help="companies read at the same time")
    a = ap.parse_args()
    base, H = os.environ["AIRTABLE_BASE"], {"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}"}
    run = qa.Run("anchor_capture_eval.py")
    pages, held = fetch_inputs(H, base, a.companies)
    print(f">>> {len(pages):,} companies, {sum(len(p) for p in pages.values()):,} report pages", flush=True)
    deadline = time.time() + a.budget * 60
    rows, counters, lock = [], collections.Counter(), threading.Lock()

    def one(wba):
        try:
            r, c = company_run(wba, pages[wba], held[wba], a.per_page, deadline)
        except Exception as e:                 # one odd site never ends the run
            r, c = [], collections.Counter({f"company failed ({type(e).__name__})": 1})
        with lock:
            rows.extend(r); counters.update(c)
            counters["companies done"] += 1
            done = counters["companies done"]
            if done % 25 == 0:
                print(f"  {done:,}/{len(pages):,} companies, {sum(1 for x in rows if x['find']):,} finds", flush=True)

    with ThreadPoolExecutor(a.workers) as ex:
        list(ex.map(one, list(pages)))
    cut = time.time() > deadline
    fields = ["wba_id", "company", "page", "page_type", "link", "link_text", "address_rules_take_it", "answer",
              "find", "found_pdf", "found_text", "type_named", "label_year"]
    with open("anchor_capture.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader(); w.writerows(rows)
    md = summary(rows, counters, len(pages), cut)
    open("anchor_capture.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    run.finish("partial" if cut else "ok", rows_in=counters["named links opened"],
               rows_out=sum(1 for r in rows if r["find"] in ("direct", "one hop")), counters=dict(counters))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("anchor_capture.md", "w").write("### Link text for capture: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
