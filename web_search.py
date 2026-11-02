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
  - it is on one of the company's sites (its website and the domains of its monitored pages); a file on a
    document host that serves many organisations' files (DOC_HOSTS: q4cdn, HKEXnews, CDNs...) is always
    opened and checked like a file on another site (2 Nov 2026);
  - its title, address or snippet names the family, in English or one of the common report languages;
  - it names one of the missing years.
Passed documents go to companies.research_link, so the inbox (inbox.py, every 30 minutes) adds them
with its duplicate and tombstone guards, attaches the file and has it typed and dated like any other
document, marked as research. At most MAX_PER_FAMILY per family and company.

Every candidate is opened before it is passed on (2 Nov 2026: 37 of 167 documents brought in by the waves of
1 and 2 Nov were other organisations' files or not reports; own-site files had never been opened). The
quick test on the title and address only sorts results out (not a report, no family word, no missing year)
and classes the site:
  own        the company's sites: the cover must show the company, the report and a missing year, 8+ pages
  doc host   a CDN, filing system, IR platform or mirror (DOC_HOSTS): also the whole name on the title page
  other      any other site: also the name heading the file (first lines, properties or file name)
  never      a government site, an academic repository, a foundation, a registry, a .org: not opened
On every site a file is refused when its first lines say it is something else (an opinion, a rating, a
release, a fund report), when its first pages say someone else wrote it, or when the company's name is
printed only as part of a subsidiary's or affiliate's name (McCain Foods India, Lactalis Danmark A/S).
The rest are listed in web_search_note with the reason.

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

# Hosts that serve many organisations' documents from a domain that is not the company's own: CDNs and
# storage, investor-relations platforms, filing systems, news wires, site builders' asset hosts and report
# mirrors. A file there is never taken on its address (1 Nov 2026: an HKEXnews PDF is named by a number;
# 2 Nov 2026: Football Association of Wales files on amazonaws.com were passed for FAW the carmaker): it is
# opened and its title page must carry the company's whole name.
DOC_HOSTS = {"q4cdn.com", "hkexnews.hk", "mziq.com", "sitecorecloud.io", "cision.com", "globenewswire.com",
             "prnewswire.com", "amazonaws.com", "cloudfront.net", "azureedge.net", "windows.net", "googleapis.com",
             "digitaloceanspaces.com", "ctfassets.net", "website-files.com", "wixstatic.com", "squarespace.com",
             "hubspotusercontent-na1.net", "hubspotusercontent-eu1.net", "sec.gov", "cninfo.com.cn", "sse.com.cn",
             "szse.cn", "tdnet.info", "jpx.co.jp", "edinet-fsa.go.jp", "dart.fss.or.kr", "bseindia.com", "nseindia.com",
             "londonstockexchange.com", "euronext.com", "asx.com.au", "annualreports.com", "responsibilityreports.com",
             "companiesmarketcap.com", "craft.co", "issuu.com", "yumpu.com", "scribd.com", "publicnow.com",
             "marketscreener.com"}
# Domains that are never a company's own site and never hold its report (1 Nov 2026 dry run: a monitored SBTi
# page made sciencebasedtargets.org "the company's site", and its glossary was passed on as an annual report):
# initiatives, social networks, site builders' home domains.
NOT_COMPANY = {"wix.com", "hubspot.com", "wordpress.com", "sciencebasedtargets.org", "lobbymap.org", "weps.org",
               "unglobalcompact.org", "cdp.net", "globalreporting.org", "linkedin.com", "google.com", "youtube.com",
               "facebook.com", "x.com", "twitter.com", "wwf.de", "wwf.org", "panda.org", "theclimategroup.org"}
# Registered domains shared by many organisations, like a government's: a company's site under one is its
# host, not the domain (2 Nov 2026: Axis Bank's and SBI's reports passed for Bank of India, all under bank.in).
SHARED_SLD = {"bank.in"}
# Company registries: their files are legal entities' accounts, a foreign group's local subsidiary as often as
# the group (2 Nov 2026: Lactalis Danmark, Remondis A/S and SC Johnson Professional A/S from the Danish registry).
REGISTRIES = {"cvrapi.dk", "virk.dk", "companieshouse.gov.uk", "brreg.no", "bolagsverket.se", "handelsregister.de",
              "infogreffe.fr", "registroimprese.it", "kvk.nl", "zefix.ch"}
# In a domain's label: a foundation, an institute or a registry is not the company (2 Nov 2026: the Digicel
# Foundation's annual reports passed for Digicel).
NOT_COMPANY_LABEL = re.compile(r"foundation|fundaci|funda[cs]ao|stiftung|fondation|fondazione|registry|institute", re.I)
# Addresses of academic repositories (2 Nov 2026: a university thesis about Mercadona passed as its report).
ACADEMIC = re.compile(r"\.edu(\.|/|$)|\.ac\.[a-z]{2,3}(/|$)|academic|/bitstreams?/|/handle/\d|dspace|repositor|/thesis|/tesis|eprints|\.uni-", re.I)
# Words in a title or file name that mark a document that is not a report.
NOT_A_REPORT = re.compile(r"press[ _-]?release|news[ _-]?release|media[ _-]?release|comunicado|communiqu|presentation|"
                          r"conference[ _-]?call|earnings|prospectus|glossary|questionnaire|consultation|memo\b|tender|"
                          r"factsheet|fact[ _-]sheet|basis[ _-]of[ _-]reporting|policy|\bq[1-4]\b|\b[1-4]q(\d\d)?\b|interim|"
                          r"half[ _-]?year|quarterly|webcast|invitation|agenda|minutes|newsletter|magazine|magazin\b|framework|"
                          r"grundsatzerkl|semi[ _-]?annual|sample|plan[ _-]?document|summary[ _-]plan|"
                          r"\bfunds?[ _-](semi[ _-]?)?annual|\betfs?\b|sicav|ucits|communication[ _-]file|"
                          r"downloadpubliccommunication|trend[ _-]?report|white[ _-]?paper|outlook[ _-]for|"
                          r"(investment|market|economic)[ _-]outlook|intimation|\bnotice\b|rating[ _-](rationale|report)|"
                          r"second[ _-]party[ _-]opinion|assurance[ _-](statement|report)|verification[ _-]statement|"
                          r"allocation[ _-](and[ _-]|&[ _-]?)?impact|use[ _-]of[ _-]proceeds|green[ _-]bond|bond[ _-](allocation|impact|report)|"
                          r"financing[ _-]report|post[ _-]issuance|(^|[ _-])cover([ _.-]|$)|result[ _-]of[ _-]monitoring|"
                          r"\bthesis\b|\btesis\b|dissertation|trabajo[ _-](de[ _-])?fin|\bforum\b|\bfoundation\b|fundaci[oó]n|"
                          r"funda[cç][aã]o|stiftung|fondation|program(me)?[ _-]annual[ _-]report|terms[ _-]of[ _-]reference", re.I)


def not_a_report(text, name=""):
    """The word in a title, a file name or a cover's first lines that says the file is not a report, or None.
    A word that is part of the company's own name does not count (Future Fund, Post Holdings)."""
    own = set(fold(name).split())
    for m in NOT_A_REPORT.finditer(text or ""):
        w = fold(m.group(0))
        if w and not any(x in own for x in w.split()):
            return m.group(0).strip(" _-").lower()
    return None


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

# ISO 3166 alpha-3 (companies.hq_country) to the country-code top-level domain.
ISO2 = {"AGO": "ao", "ARE": "ae", "ARG": "ar", "AUS": "au", "AUT": "at", "AZE": "az", "BEL": "be", "BGD": "bd", "BMU": "bm",
        "BRA": "br", "CAN": "ca", "CHE": "ch", "CHL": "cl", "CHN": "cn", "CIV": "ci", "COD": "cd", "COL": "co", "CZE": "cz",
        "DEU": "de", "DNK": "dk", "DZA": "dz", "ECU": "ec", "EGY": "eg", "ESP": "es", "EST": "ee", "ETH": "et", "FIN": "fi",
        "FRA": "fr", "GBR": "uk", "GRC": "gr", "HKG": "hk", "HUN": "hu", "IDN": "id", "IND": "in", "IRL": "ie", "IRN": "ir",
        "IRQ": "iq", "ISR": "il", "ITA": "it", "JAM": "jm", "JPN": "jp", "KAZ": "kz", "KEN": "ke", "KOR": "kr", "KWT": "kw",
        "LBY": "ly", "LIE": "li", "LUX": "lu", "MAR": "ma", "MEX": "mx", "MUS": "mu", "MYS": "my", "NGA": "ng", "NLD": "nl",
        "NOR": "no", "NPL": "np", "NZL": "nz", "OMN": "om", "PAK": "pk", "PER": "pe", "PHL": "ph", "POL": "pl", "PRT": "pt",
        "PSE": "ps", "QAT": "qa", "RUS": "ru", "SAU": "sa", "SDN": "sd", "SGP": "sg", "SWE": "se", "THA": "th", "TKM": "tm",
        "TUR": "tr", "TWN": "tw", "TZA": "tz", "UKR": "ua", "URY": "uy", "USA": "us", "VEN": "ve", "VNM": "vn", "ZAF": "za",
        "ZWE": "zw"}
# Two-letter top-level domains used as generic ones, not as a country's.
GENERIC_TLD = {"co", "io", "ai", "me", "tv", "cc", "to", "ly", "fm", "gg", "ws", "eu", "us"}
# Words that, next to the company's name, mark a local subsidiary or affiliate (novonordisk-us, McCain Foods India,
# Lactalis Danmark, Watsons Türkiye): country codes, countries, demonyms and regions, folded.
COUNTRY_MARKERS = set("""usa uk nz uae ksa
america american americas usa united states canada canadian mexico mexican brasil brazil brazilian argentina chile colombia peru
uruguay venezuela latam latin europe european deutschland germany german austria osterreich schweiz switzerland swiss france french
espana spain spanish iberia iberica iberian portugal portuguese italia italy italian nederland netherlands holland dutch belgie
belgique belgium belgian luxembourg danmark denmark danish sverige sweden swedish norge norway norwegian suomi finland finnish
nordic nordics baltic baltics polska poland polish czech cesko hungary magyar romania greece hellas ireland irish britain british
england scotland wales turkiye turkey turkish russia russian ukraine kazakhstan japan japanese nippon nihon china chinese korea
korean india indian pakistan bangladesh sri lanka indonesia malaysia singapore thailand vietnam philippines taiwan hongkong
hong kong macau australia australian zealand oceania pacific apac asean asia asian middle east gulf arabia saudi emirates uae
qatar kuwait oman bahrain israel egypt morocco algeria tunisia kenya nigeria ghana africa african overseas""".split())
# Legal forms that name a local company, with the countries where a group is itself registered under them. A
# company printed under a form of another country is a local subsidiary (Remondis A/S for a German group).
FORM_COUNTRIES = {"a s": {"DNK", "CZE", "SVK"}, "aps": {"DNK"}, "as": {"NOR", "TUR", "CZE", "SVK", "DNK", "EST", "LVA", "LTU"},
                  "asa": {"NOR"}, "ab": {"SWE", "FIN"}, "oy": {"FIN"}, "oyj": {"FIN"}, "abp": {"FIN"},
                  "gmbh": {"DEU", "AUT", "CHE", "LIE"}, "ag": {"DEU", "AUT", "CHE", "LIE"}, "kg": {"DEU", "AUT"}, "kgaa": {"DEU"},
                  "pvt": {"IND", "PAK", "LKA", "BGD", "NPL", "SGP", "ZWE", "KEN"}, "private limited": {"IND", "PAK", "LKA", "BGD", "NPL", "SGP"},
                  "pty": {"AUS", "ZAF", "NAM", "BWA"}, "sdn bhd": {"MYS"}, "bhd": {"MYS"}, "berhad": {"MYS"}, "pte": {"SGP"},
                  "tbk": {"IDN"}, "kk": {"JPN"}, "ltda": {"BRA", "COL", "CHL", "PER", "ECU", "BOL", "PRY", "URY", "VEN", "PRT",
                                                           "MOZ", "AGO", "CRI", "GTM", "HND", "NIC", "SLV", "DOM", "PAN"},
                  "lda": {"PRT", "AGO", "MOZ"}, "spa": {"ITA", "DZA"}, "srl": {"ITA", "ROU", "ARG"}, "sl": {"ESP"}, "slu": {"ESP"},
                  "sas": {"FRA", "COL"}, "sarl": {"FRA", "LUX", "CHE", "MAR", "DZA", "TUN"}, "bv": {"NLD", "BEL", "CUW", "SUR"},
                  "nv": {"NLD", "BEL", "CUW", "SUR"}, "sa de cv": {"MEX"}, "sab de cv": {"MEX"}, "pao": {"RUS"}, "ojsc": {"RUS", "UKR"},
                  "pjsc": {"RUS", "UKR", "ARE", "KWT", "QAT", "SAU", "EGY", "OMN", "BHR"},
                  "inc": {"USA", "CAN", "PHL", "JPN", "KOR", "CHN", "TWN", "HKG", "SGP", "ISR", "BMU", "CYM", "MUS", "JAM", "PAN",
                          "IRN", "IRQ", "LBY", "SDN", "ETH", "TZA", "ZWE", "CIV", "COD", "AGO", "AZE", "TKM", "LIE", "PSE", "NPL"}}
FORM_WORDS = sorted(FORM_COUNTRIES, key=len, reverse=True)
# A name made only of such words (Future Fund, National Development Fund) is shared by other organisations: off
# the company's site the file must carry its website too, as a one-word name must.
GENERIC_WORDS = set("""fund funds future national international general global united first central northern southern eastern
western pacific atlantic bank capital energy power group partners trust financial holdings industries foods motors airways
airlines oil gas steel chemical chemicals pharma health life care home land sea star sun new one world state city federal royal
imperial standard premier prime alpha delta omega apex summit horizon pioneer liberty retirement systems system investments
investment management insurance savings railways railway post telecom mobile electric water grid resources mining construction
engineering development company corporation enterprises services solutions technologies technology digital data cloud media
network networks communications pension wealth sovereign authority council board public private commercial""".split())


def site_of(url, reg_domain, path_site=True):
    """The site a company address names: its registered domain; the full host when that domain is a
    government's or a shared one (every agency or bank under it: ptc.punjab.gov.pk, bankofindia.bank.in);
    the domain plus the first path segment when the address is a folder of a bare government domain
    (michigan.gov/ors, 2 Nov 2026: another Michigan agency's annual report passed for the retirement systems)."""
    from urllib.parse import urlsplit
    if not url:
        return ""
    u = url.strip() if "://" in url else "https://" + url.strip()
    d = reg_domain(u) or ""
    if not d:
        return ""
    host = (urlsplit(u).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    if GOVERNMENT.search(d) or d in SHARED_SLD:
        if host != d:
            return host
        seg = [x for x in (urlsplit(u).path or "").split("/") if x]
        if path_site and seg and GOVERNMENT.search(d) and "." not in seg[0]:
            return d + "/" + seg[0].lower()
    return d


def on_sites(link, domains):
    """True when the link is on one of the company's sites: a registered domain (or a full host when the
    site is a subdomain of a shared one) or below it; for a site with a path (michigan.gov/ors), that host
    and a path under it."""
    from urllib.parse import urlsplit
    sp = urlsplit(link)
    host = (sp.hostname or "").lower()
    path = (sp.path or "").lower()
    for d in domains:
        dom, _, pre = d.partition("/")
        if host == dom or host == "www." + dom or (not pre and host.endswith("." + dom)):
            if not pre or path == "/" + pre or path.startswith("/" + pre + "/"):
                return True
    return False


def cctld(domain):
    """The country of a domain's top-level label (watsons.com.tr: tr), or "" for a generic one."""
    last = domain.rsplit(".", 1)[-1].lower()
    return last if len(last) == 2 and last not in GENERIC_TLD else ""


def affiliate_site(domain, label, matched, iso):
    """True when a monitored page's domain is a local affiliate's, not the group's: the company's name word
    followed by a country marker (novonordisk-us, mccainindia) or a country top-level domain that is not the
    company's own (watsons.com.tr for a Hong Kong group)."""
    rest = label.lower()
    for t in sorted(matched, key=len, reverse=True):
        rest = rest.replace(t, " ")
    words = [w for w in re.split(r"[-_ ]+", rest) if w]
    if words and all(w in COUNTRY_MARKERS or w in ISO2.values() for w in words):
        return True
    cc = cctld(domain)
    return bool(cc and iso and ISO2.get(iso) and cc != ISO2[iso])


def site_class(link, domains, reg_domain):
    """Where a result sits: "own" (the company's sites), "doc_host", "never" (a site that never holds the
    company's own report: a government's, an academic repository, a registry, a foundation's, a .org, an
    initiative's) or "third" (any other site)."""
    from urllib.parse import urlsplit
    if on_sites(link, domains):
        return "own"
    d = reg_domain(link) or ""
    host = (urlsplit(link).hostname or "").lower()
    if d in DOC_HOSTS:
        return "doc_host"
    if d in NOT_COMPANY or d in REGISTRIES or host in REGISTRIES:
        return "never"
    if GOVERNMENT.search(d) or d.endswith(".org") or ACADEMIC.search(link) or NOT_COMPANY_LABEL.search(d.split(".")[0]):
        return "never"
    return "third"


def never_why(link, reg_domain):
    d = reg_domain(link) or ""
    if d in REGISTRIES:
        return f"a company registry ({d})"
    if d in NOT_COMPANY:
        return f"not a company's site ({d})"
    if ACADEMIC.search(link):
        return f"an academic repository ({d})"
    if NOT_COMPANY_LABEL.search(d.split(".")[0]):
        return f"a foundation's or an institute's site ({d})"
    if GOVERNMENT.search(d):
        return f"a government site ({d})"
    return f"a .org site ({d})"


def company_domains(f, page_urls, reg_domain):
    """The company's own sites: its website, then the domains of its monitored pages whose name shares a word
    with the company's name or the website's (most pages first). Never a document host or a NOT_COMPANY
    domain, never a government one. At most 4. A website that is a subdomain of a government domain
    (ptc.punjab.gov.pk, metro.cdmx.gob.mx) counts as that host, not the whole domain: the domain is shared by
    every agency of that government (1 Nov 2026 wave: three other Punjab agencies' annual reports and a
    civil-protection notice about a running race passed as the company's)."""
    out = []
    s = site_of(f.get("website") or "", reg_domain)
    if s and s.split("/")[0] not in DOC_HOSTS and s.split("/")[0] not in NOT_COMPANY:
        out.append(s)
    tokens = set(name_tokens(f.get("company_name"))) | ({out[0].split(".")[0].split("/")[0]} if out else set())
    iso = (f.get("hq_country") or "").strip().upper() or None
    for d, _ in collections.Counter(site_of(u, reg_domain) for u in page_urls if u).most_common():
        base = d.split("/")[0]
        if not d or d in out or base in DOC_HOSTS or base in NOT_COMPANY or base in REGISTRIES or len(out) >= 4:
            continue
        if GOVERNMENT.search(base) and "/" not in d and base == (reg_domain("https://" + base) or ""):
            continue                        # a whole government domain is never the company's
        label = base.split(".")[0]
        parts = re.split(r"[-_]", label)
        matched = [t for t in tokens if len(t) >= 3 and (label.startswith(t) or t in parts or (len(t) >= 5 and t in label))]
        if not matched or NOT_COMPANY_LABEL.search(label) or affiliate_site(base, label, matched, iso):
            continue
        out.append(d)
    return out


LEGAL_STOP = {"the", "and", "group", "holding", "holdings", "company", "corp", "corporation", "inc", "ltd", "limited",
              "plc", "sa", "ag", "nv", "se", "spa", "co", "of", "asa", "ab", "oyj", "bhd", "tbk", "pt", "berhad", "s.a.",
              "a/s", "as", "oy", "gmbh", "llc", "lp", "bv", "sas", "kg", "sca", "pte", "pty", "pvt", "sdn", "ltda", "srl"}


def name_tokens(name):
    """The name's words of three letters or more, legal words removed: for matching domains and pages loosely."""
    return [t for t in re.findall(r"[a-z0-9]+", (name or "").lower()) if t not in LEGAL_STOP and len(t) > 2]


def core_name(name):
    """The name without its legal words, every other word kept, folded: "SC Johnson" is "sc johnson", not
    "johnson" (2 Nov 2026: Johnson Outdoors' annual report passed for SC Johnson on the word "johnson")."""
    return " ".join(t for t in fold(name).split() if t not in LEGAL_STOP)


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
    cls = site_class(link, domains, reg_domain)
    if cls == "never":
        return False, "not the company's: " + never_why(link, reg_domain)
    if cls == "third":
        return False, f"another site ({d})"
    if cls == "doc_host" and not any(t in (title + " " + link.lower()) for t in name_tokens(name)[:2]):
        return False, f"shared host without the company's name ({d})"
    w = not_a_report(title + " " + last.replace("_", " "), name)
    if w:
        return False, f"not a report ({w})"
    if ".pdf" not in link.lower() and cls != "doc_host":
        return False, "not a PDF"
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
    if cls == "doc_host":
        # Never passed on from its title and address alone: a shared host serves every organisation's files, and a
        # short name in the address proves nothing (2 Nov 2026 wave: two Football Association of Wales files on
        # amazonaws.com and cloudfront.net, "faw" in their addresses, were passed on for FAW, the carmaker). The
        # file is opened and must pass the checks for a file on another site (cover_check, site "doc_host").
        return False, f"on a shared host, to be opened ({d})"
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


CHECK_MAX = 4                           # files from other sites opened per company and family to check the cover
CHECK_MAX_OWN = 6                       # files from the company's own sites opened per company and family
CHECK_MB = 60                           # larger files are not opened (the inbox still could, but we cannot check)
MIN_PAGES = 8                           # a report has at least this many pages; a release or a memo has fewer
CHECKABLE = ("another site", "shared host without", "on a shared host", "not a PDF", "does not name")   # and a title with no year at all


def master(path=None):
    """{wba_id: {"iso": "DNK", "corporate_name": "Novo Nordisk A/S"}} from WBA's company master list in the
    repository (data/sdg2000_2025.csv): the group's legal name says which legal form is its own."""
    import csv
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "sdg2000_2025.csv")
    out = {}
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                out[row.get("WBA ID", "").strip()] = {"iso": (row.get("Location HQ ISO Code") or "").strip(),
                                                      "corporate_name": (row.get("Corporate Name") or "").strip()}
    except OSError:
        pass
    return out


def reg_domain_of(website):
    """The registered domain of a companies.website value, or ""."""
    if not website:
        return ""
    from monitor_core import reg_domain
    w = website.strip()
    return reg_domain(w if "://" in w else "https://" + w) or ""


def fold(t):
    """Lower-case ASCII words: accents dropped, dots inside abbreviations removed ("A.S. Watson" is "as watson",
    "S.A." is "sa"), every other punctuation a space."""
    import unicodedata
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"(?<![a-z])([a-z])\.(?=[a-z])", r"\1", t)
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
                         r"registry|semi-?annual report|plan document|sample report|prepared by|on behalf of|affiliate of|"
                         r"prepared for|rating rationale|credit rating|second[- ]party opinion",
                         re.I)
# Signs, on any site, that the file is about a relative of the company, not the company (2 Nov 2026: Datang
# International Power's ESG report passed for China Datang, which it names as its parent).
OWNERSHIP = re.compile(r"\b(a|an|the) (wholly[- ]owned |indirect(ly)? |direct )?subsidiary of|controlling shareholder|"
                       r"portfolio company|member firm of|joint venture (of|between)", re.I)
# Signs, in a file's first lines or its name, that it is something else (2 Nov 2026: a DNV second-party
# opinion, an ESG rating rationale, a trend report, fund reports on the companies' own sites).
HEADLINE = re.compile(r"second[- ]party opinion|independent (limited )?assurance|assurance (statement|report)|"
                      r"verification statement|rating rationale|credit rating|press release|news release|media release|"
                      r"dissertation|\bthesis\b|\btesis\b|trabajo (de )?fin de|\btfg\b|\btfm\b|magazine|magazin\b|"
                      r"newsletter|bulletin|\betfs?\b[^.]{0,60}(semi-?)?annual report|sicav|ucits|terms of reference|"
                      r"white ?paper|trend report|outlook for|investment outlook|development program(me)? annual report|"
                      r"\bfund annual report|semi-?annual report|club licensing|anti-doping", re.I)


def headline_word(text, name=""):
    """The word in a file's first lines or name that says it is something else, or None; words of the
    company's own name do not count."""
    own = set(fold(name).split())
    for m in HEADLINE.finditer(text or ""):
        w = fold(m.group(0))
        if w and not any(x in own for x in w.split()):
            return " ".join(m.group(0).split()).lower()
    return None


def legal_form(name):
    """The legal form a folded name ends with ("novo nordisk a s": "a s"), or ""."""
    n = fold(name)
    for w in FORM_WORDS:
        if n == w or n.endswith(" " + w):
            return w
    return ""


def affiliate_name(front, name, f):
    """The affiliate's or subsidiary's name the file prints instead of the company's, or "". Every time the
    company's name (whole or without its legal words) appears on the first three pages, the words next to
    it are read: a country marker ("McCain Foods India", "Lactalis Danmark", "Watsons Türkiye"), a legal form
    of another country than the company's ("Remondis A/S" for a German group), or the group's own form
    replaced by another ("Novo Nordisk Inc" for an A/S) mark that occurrence as a relative's. The file is
    a relative's when every occurrence is (2 Nov 2026: eleven such files passed in two waves)."""
    text = " " + fold(front[:6000]) + " "
    whole, core = fold(name), core_name(name)
    iso = (f.get("hq_country") or "").strip().upper()
    own_form = legal_form(f.get("corporate_name") or name)
    own_words = set(whole.split()) | set(fold(f.get("corporate_name") or "").split())
    found, qualified = 0, ""
    for n in sorted({whole, core} - {""}, key=len, reverse=True):
        for m in re.finditer(r"(?<= )" + re.escape(n) + r"(?= )", text):
            found += 1
            after = text[m.end():].split()[:4]
            tag = ""
            for w in after[:2]:
                if w in COUNTRY_MARKERS and w not in own_words:
                    tag = w
            for i in range(min(3, len(after))):          # the form within the next words: "Professional A/S"
                tail = " ".join(after[i:])
                form = next((x for x in FORM_WORDS if tail == x or tail.startswith(x + " ")), "")
                if form:
                    countries = FORM_COUNTRIES[form]
                    if (iso and iso not in countries) or (own_form and own_form != form and form != "inc"):
                        tag = tag or form
                    break
            if not tag:
                return ""                        # a bare occurrence: the company itself
            qualified = qualified or (n + " " + " ".join(after[:2])).strip()
        if found:
            break
    return qualified if found else ""


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


def cover_check(text, pages, f, fam, years, own_site=True, site=None, title_years=None):
    """(True, why) when a file's first pages show it is the company's report of a missing year: the company's
    name (all its name words, or the whole name), the report family and a missing year, and at least MIN_PAGES
    pages. Not the cover alone (the owner's warning, 1 Nov 2026: a cover may carry only a logo): `text` is
    {"front": pages 1-3, "more": pages 4-15, "meta": the PDF's title, author and subject, "file": its name},
    or a plain string for pages 1-3. The name may be anywhere in those; the report and the year in the first
    five pages or the metadata (deeper pages print every comparison year, so they would prove nothing).

    site: "own" (the company's sites), "doc_host" (a CDN, a filing system, a mirror) or "third" (any other
    site); own_site=False means "third". title_years: the years the result's title named, used when the
    file's first pages give none (an own-site file whose cover is a picture)."""
    site = site or ("own" if own_site else "third")
    if isinstance(text, str):
        text = {"front": text}
    front, more, meta, fname = (text.get(k) or "" for k in ("front", "more", "meta", "file"))
    name = f.get("company_name") or ""
    tokens = name_tokens(name)
    whole, core = fold(name), core_name(name)
    anywhere = " " + fold(" ".join([front, more, meta, fname])) + " "
    if not whole or not (f" {whole} " in anywhere or (core and f" {core} " in anywhere)
                         or (tokens and all(f" {tok} " in anywhere for tok in tokens))):
        return False, "the company's name is not on its first 15 pages or in its properties"
    if pages is not None and pages < MIN_PAGES:
        return False, f"{pages} pages, not a report"
    if site != "own":
        # Not on the company's own site: it must be the company's report, not a document about it (1 Nov 2026
        # wave: parent's and subsidiary's reports, broker notes and studies that mention the company passed).
        # The whole name on the title page (the first 1,500 characters) or in the file's properties or name,
        # else at least twice on pages 1 to 3 (a cover may carry only a logo); never only on deeper pages.
        cover = " " + fold(front[:1500] + " " + meta + " " + fname) + " "
        names = {n for n in (whole, core) if n}
        if not any(f" {n} " in cover or (" " + fold(front) + " ").count(f" {n} ") >= 2 for n in names):
            return False, "on another site and its title page does not carry the company's whole name"
        hit = THIRD_PARTY.search(front[:3000] + " " + meta)
        if hit:
            return False, f"on another site and written by someone else ({hit.group(0).strip().lower()})"
        if site == "third":
            # An unrelated site (another company's, a news service's): the company's name must head the file,
            # in its first lines, its properties or its name (2 Nov 2026: Virgin Australia's and JD Sports'
            # reports passed for the shareholders and brands they name further down the cover).
            top = " " + fold(front[:300] + " " + meta + " " + fname) + " "
            if not any(f" {n} " in top for n in names):
                return False, "on an unrelated site and the company's name does not head the file"
        website = reg_domain_of(f.get("website"))
        generic = (len(tokens) == 1 and len(tokens[0]) <= 5) or (core and all(w in GENERIC_WORDS for w in core.split()))
        legal = fold(f.get("corporate_name") or "")
        if legal and legal != whole and f" {legal} " in cover:
            generic = False                  # the group's full legal name on the title page settles it (Dole plc)
        if generic and website and f" {fold(website)} " not in " " + fold(front + " " + more + " " + meta) + " ":
            # a short name or acronym (ADIA) or a name of common words (Future Fund) is shared by other
            # organisations: its website must be in the file
            return False, f"on another site, a shared name, and {website} is not in the file"
    hit = headline_word(front[:400] + " " + fname.replace("-", " ").replace("_", " "), name)
    if hit:
        return False, f"its first lines say it is something else ({hit})"
    hit = OWNERSHIP.search(front[:3000])
    if hit:
        return False, f"about a relative of the company ({hit.group(0).strip().lower()})"
    rel = affiliate_name(front, name, f)
    if rel:
        return False, f"a subsidiary's or affiliate's document ({rel})"
    if not report_titled(" ".join([front[:3000], meta, fname.replace("-", " ").replace("_", " ")]), fam):
        return False, "its title page does not name the report"
    # The report's own year: the one its properties or file name give, else the FIRST year printed on its first
    # page (1 Nov 2026 dry run: a 2022 report passed as 2025 because its cover named its 2025 targets).
    first = [int(y) for y in re.findall(r"(?<!\d)(20\d\d)(?!\d)", front[:1500])]
    own = years_in(meta + " " + fname) or ({first[0]} if first else set()) or years_in(more[:6000]) \
        or (set(title_years or ()) if site == "own" else set())
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

    Every candidate is opened (2 Nov 2026) and kept when its first pages pass cover_check for its site class:
    a result on the company's own sites that passes the quick test, and a result that fails it only because
    it sits on another site or a shared host, has no year or report word in its title, or is not a .pdf
    address (up to CHECK_MAX per family off the company's sites, CHECK_MAX_OWN on them). An own-site file
    that cannot be fetched from here (the site refuses robots) is passed on from its title, so the inbox and
    the office-network runner can try; a file elsewhere that cannot be opened is not."""
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
        got, opened, opened_own = 0, 0, 0
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
                cls = site_class(link, domains, reg_domain)
                title = (r.get("title") or "") + " " + file_part(link)[0].replace("_", " ")
                # a title or file name that names only other years is not opened: it says which year the file is
                tf = years_in(title)
                allowed = set(ys) | ({max(ys) + 1} if max(ys) == expected_year() else set())
                titled_other = bool(tf) and not tf & allowed
                candidate = (ok or why.startswith(CHECKABLE) or why == "not one of the missing years") \
                    and not titled_other and not not_a_report(title, name)
                room = opened_own < CHECK_MAX_OWN if cls == "own" else opened < CHECK_MAX
                if candidate and room and opener is not None:
                    if cls == "own":
                        opened_own += 1
                    else:
                        opened += 1
                    out["opened"] += 1
                    text, pages = opener(link)
                    if text is None:
                        if ok and cls == "own" and str(pages).startswith(("HTTP", "could not be read", "over ")):
                            why = f"{why}; could not be opened ({pages}), passed on from its title"
                        else:
                            ok, why = False, f"{why}; opened: {pages}"
                    else:
                        ok2, why2 = cover_check(text, pages, f, fam, ys, site=cls, title_years=tf & allowed)
                        ok, why = ok2, (why2 if ok2 else f"{why}; opened: {why2}")
                elif candidate and ok and cls == "own" and not room:
                    ok, why = False, f"{why}; not opened: {CHECK_MAX_OWN} files of this family were already opened"
                elif candidate and opener is None and not ok:
                    pass                      # nothing to open with: only a quick-test pass on the company's site counts
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
                                         "gap_pages", "overdue_reports", "web_searched_on", "web_search_note", "research_link",
                                         "hq_country"])
    legal = master()
    for r in comps:
        m = legal.get(r["fields"].get("wba_id") or "", {})
        r["fields"].setdefault("hq_country", m.get("iso"))
        r["fields"]["corporate_name"] = m.get("corporate_name")
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
