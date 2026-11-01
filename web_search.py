#!/usr/bin/env python3
"""
web_search.py: find a company's missing annual and sustainability reports with a web search (Serper,
Google results), when the robots that watch its pages and the stock-exchange filings have not brought
them in.

For each company it works out which recent years it lacks for each family:
  sustainability   Sustainability, ESG, CSR, Integrated or Impact Report; years held from `sust_years`
  annual           Annual Report, Integrated Report, 10-K; latest held from `coverage_breakdown`
Years are reporting years, as everywhere in the pipeline: the year the period ends, a fiscal year ending
January to June counting as the year before. Recent years are the three up to the latest that should be out
by now (company_coverage's expected_year: in November 2026, 2023 to 2025). A report for 2025 is published
in 2026, so 2026 is searched and accepted too, as 2025: reports are often titled by their publication year
("Sustainability Report 2026") or by a fiscal year ending in 2026; and a document we hold dated 2026 counts
as 2025.

For each family with missing years it asks one question first on the company's own sites:
    site:acme.com OR site:acme-ir.com (sustainability OR ESG OR ...) (2025 OR 2024) filetype:pdf
and, only when that finds nothing usable, one question by name:
    "Acme Group" (sustainability OR ESG OR ...) (2025 OR 2024) filetype:pdf

A result is passed on only when all three hold:
  - it is on one of the company's sites (its website and the domains of its monitored pages) or on a
    document host that serves many companies' reports (DOC_HOSTS: q4cdn, HKEXnews...);
  - its title, address or snippet names the family, in English or one of the common report languages;
  - it names one of the missing years.
Passed documents go to companies.research_link, so the inbox (inbox.py, every 30 minutes) adds them
with its duplicate and tombstone guards, attaches the file and has it typed and dated like any other
document, marked as research. At most MAX_PER_FAMILY per family and company. Results on another site
are not added on the quick test, but they are opened (up to 4 per family): when the company's name is on
the first 15 pages or in the file's properties, the report and a missing year on the first pages, and the
file has at least 8 pages, it is passed on as well (a company's report often sits on a CDN, a group site or an archive). The rest are listed in
web_search_note with the reason.

Modes (web_searched_on says when a company was last searched):
  --mode recovery   companies with a missing recent year that were never searched: the one-off sweep
  --mode gaps       companies whose coverage says a recent report is missing or overdue (gap_pages,
                    overdue_reports), searched again after RECHECK_DAYS: the standing mode
  --mode auto       recovery while any company is left to sweep, then gaps (the schedule)
The inbox handles what this passes on in its own runs; when more than INBOX_BUSY companies still hold
research_link, a run waits (writes nothing) so the inbox is never handed more than it can do in one go.
Dry run by default: nothing is written, the report says what would be added. --commit writes
research_link, web_searched_on and web_search_note. --max-queries caps the Serper credits a run may
spend (one query = one credit); --limit caps the companies (a PR runs 30); --wba picks companies.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE, SERPER_API_KEY; AIRTABLE_QA_TOKEN (optional).
Writes web_search.md (also posted on a pull request touching this file).
"""
import argparse, collections, json, os, re, sys, time, traceback
from datetime import date, timedelta

import qa

SERPER_URL = "https://google.serper.dev/search"
RECHECK_DAYS = 30                       # gaps mode: a company is searched again after this many days
INBOX_BUSY = 20                         # companies still holding research_link: the inbox has not caught up, wait
MAX_PER_FAMILY = 3                      # documents passed on per company and family in one search
YEARS_BACK = 3                          # recent years: expected_year() and the two before it
WRITE_PAUSE = 0.22

# Hosts that serve many listed companies' documents from a domain that is not the company's own. A result
# there is added only when its title or address (not its snippet) carries the company's name (1 Nov 2026: an HKEXnews PDF is
# named by a number, so without the name it can be any listed company's).
DOC_HOSTS = {"q4cdn.com", "hkexnews.hk", "mziq.com", "sitecorecloud.io", "cision.com", "globenewswire.com",
             "amazonaws.com", "cloudfront.net", "azureedge.net", "sec.gov", "cninfo.com.cn", "sse.com.cn", "szse.cn",
             "tdnet.info", "jpx.co.jp", "edinet-fsa.go.jp", "dart.fss.or.kr", "bseindia.com", "nseindia.com",
             "londonstockexchange.com", "euronext.com", "asx.com.au"}
# Domains that are never a company's own site, even when one of its monitored pages sits there (1 Nov 2026
# dry run: a monitored SBTi page made sciencebasedtargets.org "the company's site", and its glossary was
# passed on as an annual report): site builders, report archives of many companies, initiatives, governments.
NOT_COMPANY = {"squarespace.com", "wixstatic.com", "wix.com", "website-files.com", "hubspotusercontent-na1.net",
               "hubspotusercontent-eu1.net", "hubspot.com", "wordpress.com", "sciencebasedtargets.org", "lobbymap.org",
               "responsibilityreports.com", "annualreports.com", "weps.org", "unglobalcompact.org", "cdp.net",
               "globalreporting.org", "linkedin.com", "issuu.com", "yumpu.com", "scribd.com", "publicnow.com",
               "marketscreener.com", "google.com", "youtube.com", "facebook.com", "x.com", "twitter.com"}
# Words in a title or file name that mark a document that is not a report.
NOT_A_REPORT = re.compile(r"press[ _-]?release|comunicado|communiqu|presentation|conference[ _-]?call|earnings|"
                          r"prospectus|glossary|questionnaire|consultation|memo\b|tender|factsheet|fact[ _-]sheet|"
                          r"basis[ _-]of[ _-]reporting|policy|\bq[1-4]\b|\b[1-4]q\d\d|interim|half[ _-]?year|"
                          r"quarterly|webcast|invitation|agenda|minutes|newsletter|framework|grundsatzerkl|"
                          r"semi[ _-]?annual|sample|plan[ _-]?document|summary[ _-]plan|\bfunds?[ _-](semi[ _-]?)?annual|"
                          r"communication[ _-]file|downloadpubliccommunication", re.I)


FAMILIES = {
    "sustainability": {
        "query": '(sustainability OR ESG OR CSR OR "non-financial" OR "impact report" OR responsibility)',
        "words": ["sustainab", "esg", "csr", "non-financial", "nonfinancial", "impact report", "responsib",
                  "nachhaltig", "durabilit", "développement durable", "sostenib", "sustent", "duurzaam",
                  "hållbar", "bærekraft", "bæredygtig", "vastuullisuus", "zrównoważon", "sürdürülebilir",
                  "integrated report", "可持续", "社会责任", "永續", "サステナビリティ", "統合報告", "지속가능"]},
    "annual": {
        "query": '("annual report" OR "integrated report" OR "universal registration document" OR "10-K")',
        "words": ["annual report", "annual-report", "annualreport", "integrated report", "10-k", "10k",
                  "registration document", "document d'enregistrement", "rapport annuel", "geschäftsbericht",
                  "geschaftsbericht", "jahresbericht", "informe anual", "relatório anual", "relatorio anual",
                  "relazione annuale", "relazione finanziaria annuale", "jaarverslag", "årsredovisning",
                  "årsrapport", "årsberetning", "vuosikertomus", "raport roczny", "faaliyet raporu",
                  "年度报告", "年报", "年報", "有価証券報告書", "統合報告", "사업보고서", "annual financial report"]},
}


def expected_year(today=None):
    today = today or date.today()
    return today.year - 1 if today.month >= 7 else today.year - 2


def recent_years(today=None):
    e = expected_year(today)
    return list(range(e, e - YEARS_BACK, -1))


def years_in(text):
    return {int(y) for y in re.findall(r"(?<!\d)(20\d\d)(?!\d)", text or "")}


def held_years(f):
    """{"sustainability": {years}, "annual": {years}} from the coverage fields. Annual: only the latest
    per type is known, so every year up to the latest counts as held."""
    sust = years_in(f.get("sust_years") or "")
    latest_annual = 0
    for line in (f.get("coverage_breakdown") or "").splitlines():
        t = line.split(":")[0].strip()
        m = re.search(r"latest (\d{4})", line)
        if m and t in ("Annual Report", "Integrated Report", "10K Form"):
            latest_annual = max(latest_annual, int(m.group(1)))
        if m and t == "Integrated Report":
            sust.add(int(m.group(1)))
    return {"sustainability": sust, "annual": set(range(2000, latest_annual + 1)) if latest_annual else set()}


def missing(f, years):
    held = held_years(f)
    # A document dated after the latest expected year is a fiscal year that ended in January to June of the
    # current year (a 2026 report of a March year end): by the reporting-year rule it is that latest year.
    for fam in held:
        if any(y > max(years) for y in held[fam]):
            held[fam].add(max(years))
    return {fam: [y for y in years if y not in held[fam]] for fam in FAMILIES}


def in_gap(f):
    """The nightly mode's test: coverage says a recent report is missing or overdue."""
    gaps = set(f.get("gap_pages") or []) | set(f.get("overdue_reports") or [])
    return bool(gaps & {"Sustainability report (recent)", "Annual report (recent)", "Sustainability report", "Annual report"})


GOVERNMENT = re.compile(r"\.(gov|gob|gouv|gc|go|nic|govt|org)\.[a-z]{2,3}$|\.gov$|\.(gov|gob|gouv|mil)\.|\.gc\.ca$")


def on_sites(link, domains):
    """True when the link's host is one of the company's sites (a registered domain, or a full host when the
    site is a subdomain of a shared one) or below it."""
    from urllib.parse import urlsplit
    host = (urlsplit(link).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def company_domains(f, page_urls, reg_domain):
    """The company's own sites: its website, then the domains of its monitored pages whose name shares a word
    with the company's name or the website's (most pages first). Never a document host or a NOT_COMPANY
    domain, never a government one. At most 4. A website that is a subdomain of a government domain
    (ptc.punjab.gov.pk, metro.cdmx.gob.mx) counts as that host, not the whole domain: the domain is shared by
    every agency of that government (1 Nov 2026 wave: three other Punjab agencies' annual reports and a
    civil-protection notice about a running race passed as the company's)."""
    from urllib.parse import urlsplit
    out = []
    w = (f.get("website") or "").strip()
    if w:
        u = w if "://" in w else "https://" + w
        d = reg_domain(u)
        host = (urlsplit(u).hostname or "").lower()
        if d and GOVERNMENT.search(d) and host not in (d, "www." + d):
            d = host[4:] if host.startswith("www.") else host
        if d and d not in DOC_HOSTS and d not in NOT_COMPANY:
            out.append(d)
    tokens = set(name_tokens(f.get("company_name"))) | ({out[0].split(".")[0]} if out else set())
    for d, _ in collections.Counter(reg_domain(u) for u in page_urls if u).most_common():
        if not d or d in out or d in DOC_HOSTS or d in NOT_COMPANY or re.search(r"\.gov(\.|$)|\.gob\.|\.gouv\.", d) or len(out) >= 4:
            continue
        label = d.split(".")[0]
        parts = re.split(r"[-_]", label)
        if any(label.startswith(t) or t in parts or (len(t) >= 5 and t in label) for t in tokens if len(t) >= 3):
            out.append(d)
    return out


def name_tokens(name):
    stop = {"the", "and", "group", "holding", "holdings", "company", "corp", "corporation", "inc", "ltd", "limited",
            "plc", "sa", "ag", "nv", "se", "spa", "co", "of", "asa", "ab", "oyj", "bhd", "tbk", "pt", "berhad", "s.a."}
    return [t for t in re.findall(r"[a-z0-9]+", (name or "").lower()) if t not in stop and len(t) > 2]


def query(fam, years, domains=None, name=None):
    """The search. Years are reporting years; a report for the latest one is published in the following
    year and is often titled by it ("Sustainability Report 2026", "FY2026"), so that year is asked too."""
    asked = sorted(set(years) | ({max(years) + 1} if max(years) == expected_year() else set()), reverse=True)
    ys = " OR ".join(str(y) for y in asked)
    ys = f"({ys})" if len(asked) > 1 else ys
    if domains:
        where = " OR ".join(f"site:{d}" for d in domains)
        where = f"({where})" if len(domains) > 1 else where
    else:
        where = '"' + (name or "").replace('"', "") + '"'
    return f"{where} {FAMILIES[fam]['query']} {ys} filetype:pdf"


def file_part(link):
    """The address's last segment, decoded, and the address without folders named by an upload date
    (/uploads/2025/07/, /files/2025-08/): those say when the file was put online, not the report's year."""
    from urllib.parse import unquote, urlsplit
    path = unquote(urlsplit(link).path)
    last = path.rstrip("/").rsplit("/", 1)[-1]
    folders = re.sub(r"/(?:20\d\d)[/-](?:0?[1-9]|1[0-2])(?=[/-])", "/", path.rsplit("/", 1)[0])
    folders = re.sub(r"/20\d\d-\d\d-\d\d[-\d]*", "/", folders)
    return last, folders


def judge(r, fam, years, domains, name, reg_domain):
    """(True, reason) when a search result is passed on, else (False, reason). The report and its year are
    read from the title and the file name, never from the snippet (1 Nov 2026 dry run: a 2019 report was
    passed on as 2025 because the snippet named 2025)."""
    link = r.get("link") or ""
    title = (r.get("title") or "").lower()
    last, folders = file_part(link)
    last, folders = last.lower(), folders.lower()
    d = reg_domain(link)
    if not on_sites(link, domains) and d not in DOC_HOSTS:
        return False, f"another site ({d})"
    if d in DOC_HOSTS and not any(t in (title + " " + link.lower()) for t in name_tokens(name)[:2]):
        return False, f"shared host without the company's name ({d})"
    if ".pdf" not in link.lower() and d not in DOC_HOSTS:
        return False, "not a PDF"
    if NOT_A_REPORT.search(title) or NOT_A_REPORT.search(last.replace("_", " ")):
        return False, "not a report (" + NOT_A_REPORT.search(title + " " + last.replace("_", " ")).group(0) + ")"
    named = title + " " + last + " " + folders
    if not any(w in named for w in FAMILIES[fam]["words"]):
        return False, "does not name the report"
    ys = years_in(title) | years_in(last) or years_in(folders)
    # titled by the publication year: a 2026 report of the latest expected reporting year (published in 2026,
    # or a fiscal year ending January to June 2026); the inbox's typing then dates it from the cover
    latest = max(years)
    if latest == expected_year() and latest + 1 in ys and latest not in ys:
        ys = ys | {latest}
    if not ys & set(years):
        return False, "not one of the missing years" + (f" ({', '.join(str(y) for y in sorted(ys)[-3:])})" if ys else "")
    return True, f"{fam} {max(ys & set(years))}"


class Serper:
    def __init__(self, key, max_queries, sess=None):
        import requests
        self.key, self.left, self.used, self.sess = key, max_queries, 0, sess or requests.Session()
        self.stopped = ""

    def search(self, q):
        """The organic results, or None when the budget is spent or the service refuses."""
        if self.stopped or self.left <= 0:
            self.stopped = self.stopped or "the query budget is spent"
            return None
        for attempt in range(3):
            try:
                r = self.sess.post(SERPER_URL, headers={"X-API-KEY": self.key, "Content-Type": "application/json"},
                                   data=json.dumps({"q": q, "num": 10}), timeout=30)
            except Exception:
                time.sleep(3 * (attempt + 1)); continue
            if r.status_code in (401, 402, 403):
                self.stopped = f"Serper refused the key or has no credits left (HTTP {r.status_code})"
                return None
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(5 * (attempt + 1)); continue
            self.used += 1; self.left -= 1
            return (r.json() or {}).get("organic") or [] if r.ok else []
        return []


CHECK_MAX = 4                           # files opened per company and family to check the cover
CHECK_MB = 60                           # larger files are not opened (the inbox still could, but we cannot check)
MIN_PAGES = 8                           # a report has at least this many pages; a release or a memo has fewer
CHECKABLE = ("another site", "shared host without", "not a PDF", "does not name")   # and a title with no year at all


def reg_domain_of(website):
    """The registered domain of a companies.website value, or ""."""
    if not website:
        return ""
    from monitor_core import reg_domain
    w = website.strip()
    return reg_domain(w if "://" in w else "https://" + w) or ""


def fold(t):
    import unicodedata
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


# A report's own title: a family word with a report word next to it ("Sustainability Report", "ESG-Bericht",
# "Informe de Sostenibilidad", "可持续发展报告"), not a family word alone (1 Nov 2026 wave: research notes on a
# company's own site passed as sustainability reports because they spoke of the "green transition").
REPORT_WORD = re.compile(r"report|bericht|rapport|informe|relat[oó]rio|relazione|verslag|redovisning|raport|kertomus|"
                         r"raporu|registration document|document d.enregistrement|10-k|报告|報告|报|보고서|レポート", re.I)
# Signs, on a file's first page or in its properties, that someone else wrote it: brokers, consultants,
# researchers, regulators, investors (1 Nov 2026 wave: an EY overview, a broker note, a university fellowship
# brochure, a government white paper and an exchange's guidance all passed for the company they mentioned).
THIRD_PARTY = re.compile(r"target price|price target|equity research|research report|initiat\w* coverage|"
                         r"\b(maintain|reiterate|upgrade|downgrade)\w*\s+(to\s+)?(buy|hold|sell|outperform|neutral)|"
                         r"analyst certification|briefing note|working paper|white paper|policy brief|inspector general|"
                         r"fellowship|brochure|request for information|exclusion list|case study|disclosure guidance|"
                         r"guidance for listed|dissertation|thesis|journal of|proceedings|roadmap|insights report|"
                         r"overview of|status of|in figures|outlook 20\d\d|fund annual report|etf|disclaimer: this report|"
                         r"special rapporteur|special procedures|mandates? of the|human rights council|allegation|"
                         r"\bfederation\b|confederation|trade association|industry association|charitable|attorney general|"
                         r"registry|semi-?annual report|plan document|sample report",
                         re.I)


def report_titled(text, fam):
    """True when a family word stands next to a report word (within 40 characters) in `text`."""
    low = (text or "").lower()
    for w in FAMILIES[fam]["words"]:
        if REPORT_WORD.fullmatch(w) or REPORT_WORD.search(w):
            if w in low:
                return True
            continue
        for m in re.finditer(re.escape(w), low):
            if REPORT_WORD.search(low[max(0, m.start() - 40):m.end() + 40]):
                return True
    return False


def cover_check(text, pages, f, fam, years, own_site=True):
    """(True, why) when a file's first pages show it is the company's report of a missing year: the company's
    name (all its name words, or the whole name), the report family and a missing year, and at least MIN_PAGES
    pages. Not the cover alone (the owner's warning, 1 Nov 2026: a cover may carry only a logo): `text` is
    {"front": pages 1-3, "more": pages 4-15, "meta": the PDF's title, author and subject, "file": its name},
    or a plain string for pages 1-3. The name may be anywhere in those; the report and the year in the first
    five pages or the metadata (deeper pages print every comparison year, so they would prove nothing)."""
    if isinstance(text, str):
        text = {"front": text}
    front, more, meta, fname = (text.get(k) or "" for k in ("front", "more", "meta", "file"))
    tokens = name_tokens(f.get("company_name"))
    whole = fold(f.get("company_name"))
    anywhere = " " + fold(" ".join([front, more, meta, fname])) + " "
    if not tokens or not (f" {whole} " in anywhere or all(f" {tok} " in anywhere for tok in tokens)):
        return False, "the company's name is not on its first 15 pages or in its properties"
    if pages is not None and pages < MIN_PAGES:
        return False, f"{pages} pages, not a report"
    if not own_site:
        # Not on the company's own site: it must be the company's report, not a document about it (1 Nov 2026
        # wave: parent's and subsidiary's reports, broker notes and studies that mention the company passed).
        # The whole name on the title page (the first 1,500 characters) or in the file's properties or name,
        # else at least twice on pages 1 to 3 (a cover may carry only a logo); never only on deeper pages.
        core = " ".join(tokens)                      # the name without its legal words ("Holdings Inc.")
        cover = " " + fold(front[:1500] + " " + meta + " " + fname) + " "
        if not any(f" {n} " in cover or (" " + fold(front) + " ").count(f" {n} ") >= 2 for n in {whole, core}):
            return False, "on another site and its title page does not carry the company's whole name"
        hit = THIRD_PARTY.search(front[:3000] + " " + meta)
        if hit:
            return False, f"on another site and written by someone else ({hit.group(0).strip().lower()})"
        site = reg_domain_of(f.get("website"))
        if len(tokens) == 1 and len(tokens[0]) <= 5 and site and f" {fold(site)} " not in " " + fold(front + " " + more + " " + meta) + " ":
            # a short name or acronym (ADIA) is shared by other organisations: its website must be in the file
            return False, f"on another site, a short name, and {site} is not in the file"
    if not report_titled(" ".join([front[:3000], meta, fname.replace("-", " ").replace("_", " ")]), fam):
        return False, "its title page does not name the report"
    # The report's own year: the one its properties or file name give, else the FIRST year printed on its first
    # page (1 Nov 2026 dry run: a 2022 report passed as 2025 because its cover named its 2025 targets).
    first = [int(y) for y in re.findall(r"(?<!\d)(20\d\d)(?!\d)", front[:1500])]
    own = years_in(meta + " " + fname) or ({first[0]} if first else set()) or years_in(more[:6000])
    latest = max(years)
    if latest == expected_year() and latest + 1 in own:
        own = own | {latest}
    if not own & set(years):
        return False, "no missing year on its first pages" + (f" ({', '.join(str(y) for y in sorted(own))})" if own else "")
    return True, f"{fam} {max(own & set(years))}, checked on the file's first pages"


def open_cover(link, sess=None):
    """({"front", "more", "meta", "file"}, page count) or (None, reason)."""
    import io, requests
    from pypdf import PdfReader
    sess = sess or requests.Session()
    try:
        r = sess.get(link, timeout=60, stream=True, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
        if r.status_code >= 400:
            return None, f"HTTP {r.status_code}"
        data = b""
        for chunk in r.iter_content(1 << 16):
            data += chunk
            if len(data) > CHECK_MB << 20:
                return None, f"over {CHECK_MB} MB"
        if not data.startswith(b"%PDF"):
            return None, "not a PDF"
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                pass

        def pages_text(a, b):
            out = []
            for p in reader.pages[a:b]:
                try:
                    out.append(p.extract_text() or "")
                except Exception:
                    out.append("")
            return "\n".join(out)
        try:
            m = reader.metadata or {}
            meta = " ".join(str(m.get(k) or "") for k in ("/Title", "/Author", "/Subject"))
        except Exception:
            meta = ""
        return {"front": pages_text(0, 3), "more": pages_text(3, 15), "meta": meta, "file": file_part(link)[0]}, len(reader.pages)
    except Exception as e:
        return None, f"could not be read ({type(e).__name__})"


def search_company(f, page_urls, years, api, reg_domain, opener=open_cover):
    """{"looked_for": [...], "found": [(url, why)], "not_added": [(url, why)], "queries": n, "opened": n}

    A result that fails the quick test only because it sits on another site, has no year or report word in
    its title, or is not a .pdf address is opened (up to CHECK_MAX per family) and kept when its first pages
    show the company's name, the report and a missing year (1 Nov 2026: a company's report is often on a
    CDN, a group site or an archive, and those must not be lost)."""
    name = f.get("company_name") or ""
    domains = company_domains(f, page_urls, reg_domain)
    out = {"looked_for": [], "found": [], "not_added": [], "queries": 0, "opened": 0}
    for fam, ys in missing(f, years).items():
        if not ys:
            continue
        out["looked_for"].append(f"{fam} {', '.join(str(y) for y in ys)}")
        tried = []
        if domains:
            tried.append(query(fam, ys, domains=domains))
        if name:
            tried.append(query(fam, ys, name=name))
        got, opened = 0, 0
        for q in tried:
            results = api.search(q)
            if results is None:
                out["cut"] = True
                return out
            out["queries"] += 1
            for r in results:
                link = r.get("link") or ""
                if got >= MAX_PER_FAMILY or link in {u for u, _ in out["found"]}:
                    continue
                ok, why = judge(r, fam, ys, domains, name, reg_domain)
                # a title or file name that names only other years is not opened: it says which year the file is
                tf = years_in((r.get("title") or "") + " " + file_part(link)[0])
                allowed = set(ys) | ({max(ys) + 1} if max(ys) == expected_year() else set())
                titled_other = bool(tf) and not tf & allowed
                if not ok and (why.startswith(CHECKABLE) or why == "not one of the missing years") and not titled_other \
                        and opened < CHECK_MAX and not NOT_A_REPORT.search(
                        (r.get("title") or "") + " " + file_part(link)[0].replace("_", " ")):
                    opened += 1; out["opened"] += 1
                    text, pages = opener(link)
                    if text is None:
                        why = f"{why}; opened: {pages}"
                    else:
                        ok, why2 = cover_check(text, pages, f, fam, ys, own_site=on_sites(link, domains))
                        why = why2 if ok else f"{why}; opened: {why2}"
                if ok:
                    out["found"].append((link, why)); got += 1
                elif len(out["not_added"]) < 10 and not why.startswith("does not name"):
                    out["not_added"].append((link, why))
            if got:
                break                     # the company's own sites answered: no question by name
    return out


def note(today, res):
    lines = [f"{today}: looked for {'; '.join(res['looked_for']) or 'nothing (no recent year missing)'} "
             f"({res['queries']} searches)."]
    lines += [f"  passed to research_link: {u} ({why})" for u, why in res["found"]]
    lines += [f"  not added: {u} ({why})" for u, why in res["not_added"]]
    return "\n".join(lines)


def write_companies(base, H, updates, request=None):
    """PATCH companies rows, 10 per call. request: monitor_core.airtable_request (stubbed in tests).
    Called every 10 companies during the run, so a crash later never loses searches already paid for
    (1 Nov 2026: the first wave spent 384 credits and wrote nothing when the single write at the end failed)."""
    if request is None:
        from monitor_core import airtable_request as request
    hdr = {**H, "Content-Type": "application/json"}
    for i in range(0, len(updates), 10):
        request("PATCH", f"https://api.airtable.com/v0/{base}/companies", hdr,
                payload={"records": updates[i:i + 10], "typecast": False})
        time.sleep(WRITE_PAUSE)
    return len(updates)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--mode", default="auto", choices=["auto", "gaps", "recovery"])
    ap.add_argument("--limit", type=int, default=0, help="companies at most (0 = all due)")
    ap.add_argument("--max-queries", type=int, default=300, help="Serper credits this run may spend")
    ap.add_argument("--wba", default="", help="wba ids, comma-separated (ignores the waits)")
    ap.add_argument("--minutes", type=float, default=150, help="stop searching after this (opening files takes time)")
    a = ap.parse_args()
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    key = os.environ.get("SERPER_API_KEY", "").strip()
    if not key:
        open("web_search.md", "w").write("### Web search: SERPER_API_KEY is not set (a repository secret); nothing run.\n")
        return 0
    from propose_values import fetch
    from monitor_core import reg_domain
    base, H = os.environ["AIRTABLE_BASE"], {"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}"}
    run = qa.Run("web_search.py")
    today = date.today()
    years = recent_years(today)
    comps = fetch(H, base, "companies", ["wba_id", "company_name", "website", "sust_years", "coverage_breakdown",
                                         "gap_pages", "overdue_reports", "web_searched_on", "web_search_note", "research_link"])
    wanted = {x.strip() for x in a.wba.split(",") if x.strip()}
    c = collections.Counter()
    busy = sum(1 for r in comps if (r["fields"].get("research_link") or "").strip())
    if a.commit and busy > INBOX_BUSY and not wanted:
        md = (f"### Web search for missing reports ({a.mode})\n\n{busy} companies still hold research_link from an "
              f"earlier run: the inbox has not caught up, so this run waits. Nothing searched, nothing written.\n")
        open("web_search.md", "w").write(md); print(md, flush=True)
        run.finish("skipped", rows_in=0, rows_out=0, counters={"inbox busy": busy})
        return 0

    def due_for(mode):
        out = []
        for r in comps:
            f = r["fields"]
            if not f.get("wba_id"):
                continue
            if wanted:
                if f["wba_id"] in wanted:
                    out.append(r)
                continue
            if not any(missing(f, years).values()):
                continue
            last = f.get("web_searched_on")
            if mode == "recovery" and not last:
                out.append(r)
            elif mode == "gaps" and in_gap(f) and (not last or (today - date.fromisoformat(last[:10])).days >= RECHECK_DAYS):
                out.append(r)
        return out

    mode = a.mode
    due = due_for("recovery" if mode in ("auto", "recovery") else "gaps")
    if mode == "auto":
        mode = "recovery" if due else "gaps"
        due = due or due_for("gaps")
    c["companies missing a recent year"] = sum(1 for r in comps if r["fields"].get("wba_id") and any(missing(r["fields"], years).values()))
    c["companies never searched (left to sweep)"] = sum(1 for r in comps if r["fields"].get("wba_id") and not r["fields"].get("web_searched_on")
                                                       and any(missing(r["fields"], years).values()))
    # the companies missing the most first, then a stable order
    due.sort(key=lambda r: (-sum(len(v) for v in missing(r["fields"], years).values()), r["fields"]["wba_id"]))
    if a.limit and not a.commit:          # a dry run's sample is spread over the companies, not the hardest first
        import hashlib
        due.sort(key=lambda r: hashlib.sha1(r["fields"]["wba_id"].encode()).hexdigest())
    if a.limit:
        due = due[:a.limit]
    c["companies due this run"] = len(due)
    print(f">>> {len(due)} companies to search ({mode}); years {years}", flush=True)
    links = fetch(H, base, "monitored_links", ["wba_id", "url"],
                  "OR(" + ",".join(f"{{wba_id}}='{r['fields']['wba_id']}'" for r in due[:150]) + ")") if 0 < len(due) <= 150 else \
        fetch(H, base, "monitored_links", ["wba_id", "url"])
    pages = collections.defaultdict(list)
    for l in links:
        pages[l["fields"].get("wba_id")].append(l["fields"].get("url") or "")
    api = Serper(key, a.max_queries)
    updates, examples, written = [], [], 0
    t0 = time.time()
    for i, r in enumerate(due, 1):
        if time.time() - t0 > a.minutes * 60:
            c["stopped at the time budget"] = 1
            break
        f = r["fields"]
        res = search_company(f, pages.get(f["wba_id"], []), years, api, reg_domain)
        if res.get("cut"):
            break                        # cut by the budget: not marked as searched, picked up next run
        c["companies searched"] += 1
        c["companies with a document found"] += bool(res["found"])
        c["documents passed on"] += len(res["found"])
        c["files opened to check the cover"] += res.get("opened", 0)
        c["passed after checking the cover"] += sum(1 for _, w in res["found"] if "checked on" in w)
        for _, why in res["found"]:
            c["found: " + why.split()[0]] += 1
        for _, why in res["not_added"]:
            c["not added: " + re.sub(r" \(.*", "", why)] += 1
        if len(examples) < 40 and (res["found"] or res["not_added"]):
            examples.append(f"**{f['wba_id']} {f.get('company_name', '')}**\n\n```\n{note(today.isoformat(), res)}\n```")
        upd = {"web_searched_on": today.isoformat(),
               "web_search_note": (note(today.isoformat(), res) + "\n\n" + (f.get("web_search_note") or ""))[:20000]}
        if res["found"]:
            upd["research_link"] = "\n".join([x for x in [(f.get("research_link") or "").strip()] if x] + [u for u, _ in res["found"]])
        updates.append({"id": r["id"], "fields": upd})
        if a.commit and len(updates) >= 10:
            written += write_companies(base, H, updates); updates = []
        if i % 50 == 0:
            print(f"  {i}/{len(due)}: {api.used} searches, {c['documents passed on']} documents", flush=True)
    c["searches (credits)"] = api.used
    if api.stopped:
        c["stopped: " + api.stopped] = 1
    if a.commit and updates:
        written += write_companies(base, H, updates); updates = []
    c["companies written"] = written
    md = [f"### Web search for missing reports ({mode})" + ("" if a.commit else " (dry run: nothing written)"), "",
          f"Recent years: {', '.join(str(y) for y in years)}. Serper searches used: {api.used} (one credit each)."
          + (f" Stopped: {api.stopped}." if api.stopped else ""), "", "| | |", "| --- | --: |"]
    md += [f"| {k} | {v:,} |" for k, v in sorted(c.items())]
    if c["companies searched"]:
        md += ["", f"Per company searched: {api.used / c['companies searched']:.1f} searches, "
                   f"{c['documents passed on'] / c['companies searched']:.2f} documents. A full sweep of "
                   f"{c['companies missing a recent year']:,} companies would use about {api.used / c['companies searched'] * c['companies missing a recent year']:,.0f} searches."]
    md += ["", "#### Examples (first 40 companies with a result)", ""] + examples
    text = "\n".join(md) + "\n"
    open("web_search.md", "w").write(text)
    print(text, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(text)
    run.finish("ok" if not api.stopped or "budget" in api.stopped else "partial",
               rows_in=len(due), rows_out=c["documents passed on"], counters=dict(c))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("web_search.md", "w").write("### Web search: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
