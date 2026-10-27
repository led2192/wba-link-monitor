#!/usr/bin/env python3
"""
pub_dates.py: when each key report was published, to the day when the evidence allows, else to the month
(owner's request, 24 Oct 2026).

Scope: annual, integrated, 10-K, sustainability-family reports, registration documents and financial
statements whose year is 2023 or later, not discarded.

Evidence, strongest first (the first that applies decides; all dates must fall after the end of the
period the report covers and no later than the day the robot first saw the document):
    1 regulator filing   the filing date the SEC or ESEF database gives (regulator_filings.py notes it as
                         "filed YYYY-MM-DD" in Reviewer notes): the publication date itself. Also for a 10-K,
                         20-F or 40-F taken from the company's own site (26 Oct 2026: Macy's 10-K for the
                         year ended 31 January 2026, hosted on its investor site, was dated by first seen,
                         18 July 2026; EDGAR says filed 27 March 2026): the company's SEC filings (CIK from
                         the address or companies.sec_cik) are read and the original annual filing whose
                         period of report is the document's period end, or whose accession number the
                         address or title names, gives the date.
    1b signature         a 10-K, 20-F or 40-F with no filing found: the date under the registrant's
                         signature ("has duly caused this report to be signed on its behalf ... Date: March
                         27, 2026"), which is the filing day or a day or two before.
    2 publication        the document says so: "Date of publication: 4 March 2025", "Published in March
                         2025", "This report was published on ...", "Release date ...".
    3 board approval     the date the board approved the report or authorised the financial statements
                         for issue: "authorised for issue by the Board of Directors on 27 February 2025",
                         "approved by the Board on ...". Usually the publication day or a few weeks
                         before it; pub_date_source says so, and the measurement on regulator filings
                         (below) says by how much.
    4 first seen         for a document from the Wayback Machine (wayback_backfill.py, "(captured
                         YYYY-MM-DD)" in Reviewer notes): the day the archive first captured the file,
                         an upper bound on the publication day; else the day the robot first saw the
                         document on a page it was already watching:
                         an upper bound, used only for documents first seen after FIRST_SEEN_FROM (before
                         that date everything was first seen at the initial collection), within 18 months
                         of the period end, and found on a watched page (source_page) that had been
                         watched for at least PAGE_WATCHED_DAYS before, and not when the file's own dates
                         (file_created, file_modified, from file_dates.py) are more than FILE_AGE_DAYS older
                         than the day it was first seen. A file found on a page the robot had just started
                         watching, or a file made months before the robot met it, was there before; that
                         day says nothing about its publication.
The text is the full text (text store) when the document is in it, else the first pages (cover_text).
English phrases only in this first version, with day-month-year, month-day-year, ISO and "Month YYYY"
dates.

Writes (with --commit) on report_library: pub_date, pub_date_precision (day / month), pub_date_source,
pub_date_evidence ("p.<page>: <the sentence>"). A row whose pub_date_source is "person" is never touched.
Dry run by default: prints the distribution and, for regulator filings that also have a board approval
date in their text, how many days separate the two. Writes pub_dates.md.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE, TEXT_STORE_REPO + TEXT_STORE_TOKEN (or TEXT_STORE_DIR); QA optional.
"""
import argparse, collections, datetime as dt, gzip, json, os, re, tempfile, time

import qa
from monitor_core import airtable_request

API = "https://api.airtable.com/v0"
TYPES = {"Sustainability Report", "ESG Report", "CSR Report", "Integrated Report", "Impact Report", "Annual Report",
         "10K Form", "Registration Document", "Financial Statement"}
YEAR_MIN = 2023
FIRST_SEEN_FROM = dt.date(2026, 7, 15)
PAGE_WATCHED_DAYS = 14
FILE_AGE_DAYS = 30
FIELDS = ["library_id", "source_type_check", "source_type", "discard", "year", "period_end", "first_seen",
          "Reviewer notes", "document_url", "doc_label", "wba_id", "source_page", "file_created", "file_modified",
          "pub_date", "pub_date_precision", "pub_date_source", "pub_date_evidence"]
SOURCES = {1: "regulator filing", 5: "signature", 2: "publication", 3: "board approval", 4: "first seen"}
ORDER = (1, 5, 2, 3, 4)          # strongest first; 5 (signature) was added later, hence the number

MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9
MON = r"(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)\.?"
DATE = (r"(?:(?P<d1>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + MON.replace("(", "(?P<m1>", 1) + r",?\s+(?P<y1>20\d\d)"
        r"|" + MON.replace("(", "(?P<m2>", 1) + r"\s+(?P<d2>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<y2>20\d\d)"
        r"|(?P<y3>20\d\d)-(?P<m3>\d\d)-(?P<d3>\d\d)"
        r"|(?P<d4>\d{1,2})[./](?P<m4>\d{1,2})[./](?P<y4>20\d\d)"
        r"|" + MON.replace("(", "(?P<m5>", 1) + r",?\s+(?P<y5>20\d\d))")
# The document has to be talking about itself. A bare "published on" also dates press releases, delegated
# acts and bond notices quoted in the text (24 Oct 2026 dry run), so a publication date counts only when
#   (a) the sentence names this report: "This report was published on 16 April 2024", "The Sustainability
#       Report of X covers 1 January to 31 December 2024 and was published on 19 March 2025"; or
#   (b) a "Date of publication:" / "Publication date:" / "Published:" label sits on the first COLOPHON_PAGES
#       or the last COLOPHON_PAGES pages (the imprint), not next to a bond.
SELF = (r"(?:this|the|our)\s+(?P<subj>(?:[\w&'’-]+\s+){0,4}?)(?:report|document|publication|review|statement|"
        r"accounts)\b[^.]{0,220}?\b(?:was|is|has\s+been|were)\s+(?:first\s+)?(?:published|released|issued|made\s+available)"
        r"(?:\s+(?:on|in))?\s*")
PUBLISHED = re.compile(SELF + r"(?:" + DATE + ")", re.I)
OTHER_REPORT = re.compile(r"\b(?:prior|previous|preceding|last\s+year['’]?s?|next|upcoming|first)\b", re.I)
# "...and serves as a continuation of the 2024 Sustainability Report, which was published in March 2025"
# (Bank Mandiri, 2025 report, 26 Oct 2026): the sentence dates the report this one follows.
FOLLOWS = re.compile(r"\b(?:continuation|continues|follows|following|successor|supersed\w*|replac\w*|update)\s+(?:of|to|on)?\s*$", re.I)
YEAR = re.compile(r"\b(20\d\d)\b")


def names_older_report(subject, doc_year):
    """True when the report the sentence names carries a year before this document's year ("the 2024
    Sustainability Report" inside a 2025 report): it is the earlier edition, not this one."""
    if not doc_year:
        return False
    years = [int(y) for y in YEAR.findall(subject or "")]
    return bool(years) and max(years) < int(doc_year)
LABEL = re.compile(r"(?:date\s+of\s+publication|publication\s+date|published|release\s+date)\s*[:\-–]\s*(?:" + DATE + ")", re.I)
COLOPHON_PAGES = 4
BOND = re.compile(r"bond|notes?\b|issuance|coupon|debenture", re.I)
# Board approval of THIS report or its accounts, not of by-laws, a dividend or a transaction.
OBJECT = (r"(?:financial\s+statements|accounts|annual\s+report|strategic\s+report|integrated\s+report|"
          r"sustainability\s+(?:report|statement)|directors['’]?\s+report|management\s+report|this\s+report|the\s+report)")
APPROVED = re.compile(OBJECT + r"[^.]{0,160}?(?:approved|authori[sz]ed)(?:\s+for\s+(?:issue|publication))?[^.]{0,100}?"
                      r"\b(?:on|dated)\s+(?:" + DATE + ")", re.I)
APPROVED_BEFORE = re.compile(r"\b(?:on\s+)?(?:" + DATE + r"),?\s+the\s+(?:board|directors|supervisory\s+board)[^.]{0,40}?"
                             r"(?:approved|authori[sz]ed)\s+(?:the|these|this|our)\s+(?:[\w-]+\s+){0,3}?" + OBJECT, re.I)
# The period the report says it covers: "for the period from 1 January to 31 December 2025", "for the period
# 1 April 2025 to 31 March 2026", "for the year ended 31 March 2026". Its end is a floor for the publication
# date when Airtable has no period_end (26 Oct 2026, owner's suggestion after Bank Mandiri and SSE).
DAY_MON = (r"(?P<sd>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + MON.replace("(", "(?P<sm>", 1) + r"(?:,?\s+(?P<sy>20\d\d))?")
# A twelve-month span: "from 1 January to 31 December 2025", "1 April 2025 to 31 March 2026". Both ends must be
# written out and about a year apart, so "until 31 December 2025" (a plan) or "compared to 31 December 2024"
# (a comparison) never counts.
PERIOD_SPAN = re.compile(r"\b(?:from\s+)?" + DAY_MON + r"\s*(?:to|until|through|till|–|-)\s*(?:" + DATE + ")", re.I)
# The span has to be the report's period, not a bond's interest period or a term of office (Hua Xia Bank 2023:
# "the interest accrual period ... is from 26 June 2023 to 25 June 2024").
SPAN_ABOUT_REPORT = re.compile(r"report|covers|reporting|financial\s+year|fiscal\s+year|statements?\b|accounts", re.I)
SPAN_OTHER = re.compile(r"interest|bond|accrual|coupon|dividend|term\s+of\s+office|supervision|tenure|loan|lease|contract", re.I)
# "for the year ended 31 March 2026": past tense only; "year ending March 2026" is usually a forecast.
YEAR_ENDED = re.compile(r"\byear\s+ended\s+(?:on\s+)?(?:" + DATE + ")", re.I)
FILED = re.compile(r"\(filed (20\d\d-\d\d-\d\d)\)")
CAPTURED = re.compile(r"\(captured (20\d\d-\d\d-\d\d)\)")
# SEC filings named in a document's address or title: the company (CIK) and the filing (accession number).
CIK_IN_URL = re.compile(r"(?:CIK[-_=]?|/edgar/data/)0*(\d{1,10})(?!\d)", re.I)
ACCESSION = re.compile(r"(?<!\d)(\d{10})-?(\d{2})-?(\d{6})(?!\d)")
SEC_COVER = re.compile(r"securities\s+and\s+exchange\s+commission.{0,600}?form\s+(10-k|20-f|40-f)\b", re.I | re.S)
SEC_FORMS = {"10-K", "20-F", "40-F"}
# "the registrant has duly caused this report to be signed on its behalf by the undersigned ... Date: March 27, 2026"
SIGNED = re.compile(r"duly\s+caused(?:\s+and\s+authori[sz]ed)?\s+(?:this\s+(?:annual\s+)?report\s+to\s+be\s+signed|the\s+undersigned\s+to\s+sign)", re.I)
SIGNED_DATE = re.compile(r"\bDated?\s*:?\s*(?:" + DATE + ")", re.I)
KEYWORDS = re.compile(r"publi|release|issue|authori|approved", re.I)


def parse(m):
    """(date, 'day' | 'month') from a DATE match, or (None, None) when it is not a real date."""
    g = m.groupdict()
    try:
        if g.get("y1"):
            return dt.date(int(g["y1"]), MONTHS[g["m1"].lower().rstrip(".")], int(g["d1"])), "day"
        if g.get("y2"):
            return dt.date(int(g["y2"]), MONTHS[g["m2"].lower().rstrip(".")], int(g["d2"])), "day"
        if g.get("y3"):
            return dt.date(int(g["y3"]), int(g["m3"]), int(g["d3"])), "day"
        if g.get("y4"):
            return dt.date(int(g["y4"]), int(g["m4"]), int(g["d4"])), "day"
        if g.get("y5"):
            return dt.date(int(g["y5"]), MONTHS[g["m5"].lower().rstrip(".")], 1), "month"
    except (ValueError, KeyError):
        pass
    return None, None


def period_end_date(period_end, year):
    """Last day of the period: '2025-03' -> 2025-03-31; '2025' or a bare year -> 2025-01-01 (only the year is
    known). The year alone cannot give a tighter floor: many companies label a report with its publication
    year (a "2025" report covering 2024, published in March 2025), so a floor of 31 March 2025 removed correct
    dates (26 Oct 2026 dry run: 43 dates lost, among them a board approval on 30 March 2026 for a 2026-labelled
    report). The period the report states about itself (stated_period_end) is the floor instead.
    """
    m = re.match(r"^(20\d\d)(?:-(\d\d))?", str(period_end or ""))
    if m and m.group(2):
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            nxt = dt.date(y + (mo == 12), mo % 12 + 1, 1)
            return nxt - dt.timedelta(days=1)
    y = int(m.group(1)) if m else (int(year) if isinstance(year, (int, float)) else None)
    return dt.date(y, 1, 1) if y else None


def plausible(d, end, seen):
    if not d:
        return False
    if end and d <= end:
        return False
    if seen and d > seen:
        return False
    if end and (d - end).days > 548:
        return False
    return True


def sentence(text, i, j):
    lo = max(text.rfind(".", 0, i) + 1, i - 160)
    hi = text.find(".", j)
    hi = j + 80 if hi == -1 else min(hi + 1, j + 160)
    return re.sub(r"\s+", " ", text[lo:hi]).strip()


def find_dates(pages, end, seen, doc_year=None):
    """Best (tier, date, precision, evidence) from the text, tiers 2 (publication) and 3 (board approval)."""
    found = {}
    nums = sorted(p for p, _ in pages)
    edge = set(nums[:COLOPHON_PAGES]) | set(nums[-COLOPHON_PAGES:])
    for p, text in pages:
        if not text or not KEYWORDS.search(text):
            continue
        rules = [(2, PUBLISHED), (3, APPROVED), (3, APPROVED_BEFORE)] + ([(2, LABEL)] if p in edge else [])
        for tier, rx in rules:
            for m in rx.finditer(text):
                if rx is LABEL and BOND.search(text[max(0, m.start() - 120):m.end() + 60]):
                    continue
                if rx is PUBLISHED and OTHER_REPORT.search(m.group(0)[:80]):
                    continue            # "the prior 2024 Economic Impact Report, which was published April 2024"
                if rx is PUBLISHED and (names_older_report(m.group("subj"), doc_year)
                                        or FOLLOWS.search(text[max(0, m.start() - 40):m.start()])):
                    continue            # "a continuation of the 2024 Sustainability Report, which was published in March 2025"
                d, prec = parse(m)
                if not plausible(d, end, seen):
                    continue
                cur = found.get(tier)
                # a day beats a month; among days the latest publication / earliest approval is not
                # better than the first one stated, so the first plausible one wins
                if cur is None or (cur[1] == "month" and prec == "day"):
                    found[tier] = (d, prec, f"p.{p}: {sentence(text, m.start(), m.end())[:300]}")
    for tier in (2, 3):
        if tier in found:
            d, prec, ev = found[tier]
            return tier, d, prec, ev
    return None


def stated_period_end(pages, year=None, seen=None):
    """Latest end of the twelve-month period the text states, or None. Only ends in the document's year or the
    year after (a 2025 report covering April 2025 to March 2026), and before the robot saw the file."""
    best = None
    for _, text in pages:
        if not text:
            continue
        cands = []
        for m in PERIOD_SPAN.finditer(text):
            end, prec = parse(m)
            if not end or prec != "day":
                continue
            try:
                sm = MONTHS[m.group("sm").lower().rstrip(".")]
                sy = int(m.group("sy")) if m.group("sy") else (end.year if (sm, int(m.group("sd"))) <= (end.month, end.day) else end.year - 1)
                start = dt.date(sy, sm, int(m.group("sd")))
            except (ValueError, KeyError, TypeError):
                continue
            before = text[max(0, m.start() - 150):m.start()]
            if 330 <= (end - start).days <= 400 and SPAN_ABOUT_REPORT.search(before) and not SPAN_OTHER.search(before):
                cands.append(end)
        for m in YEAR_ENDED.finditer(text):
            end, prec = parse(m)
            if end and prec == "day":
                cands.append(end)
        for d in cands:
            if year and not (int(year) <= d.year <= int(year) + 1):
                continue
            if seen and d >= seen:
                continue
            if best is None or d > best:
                best = d
    return best


def doc_year(f):
    try:
        return int(str(f.get("year"))[:4])
    except (TypeError, ValueError):
        return None


def is_sec_form(f, pages):
    if eff_type(f) == "10K Form":
        return True
    first = " ".join(t for p, t in sorted(pages)[:3] if t)
    return bool(SEC_COVER.search(first[:20000]))


def sec_cik(f, company_cik=None):
    m = CIK_IN_URL.search(f.get("document_url") or "")
    if m:
        return str(int(m.group(1)))
    return company_cik if company_cik and company_cik != "none" else None


def sec_match(f, end, filings, seen):
    """(date, evidence) of the SEC annual filing this document is, or None. filings: [(form, accession,
    report_date, filing_date)] of the company. The accession number in the address or title decides when
    there is one; else the original annual filing whose period of report is in the same month as the period
    end."""
    if not filings:
        return None
    named = {"".join(m.groups()) for m in ACCESSION.finditer(f"{f.get('document_url') or ''} {f.get('doc_label') or ''}")}
    pick = None
    for form, acc, rep_date, filed in filings:
        if named and acc.replace("-", "") in named:
            pick = (form, acc, rep_date, filed); break
    if pick is None and end and end.month and end.day > 1:
        for form, acc, rep_date, filed in filings:
            if form in SEC_FORMS and rep_date and rep_date[:7] == end.isoformat()[:7]:
                pick = (form, acc, rep_date, filed); break
    if pick is None:
        return None
    form, acc, rep_date, filed = pick
    try:
        d = dt.date.fromisoformat(filed[:10])
    except ValueError:
        return None
    if end and d <= end or seen and d > seen:
        return None
    return d, f"SEC EDGAR: Form {form} for the period ended {rep_date}, filed {filed[:10]} (accession {acc})"


def signed_date(pages, end, seen):
    """(date, precision, evidence) under the registrant's signature in a 10-K / 20-F / 40-F, or None."""
    for p, text in pages:
        if not text:
            continue
        for m in SIGNED.finditer(text):
            window = text[m.end():m.end() + 1500]
            dm = SIGNED_DATE.search(window)
            if not dm:
                continue
            d, prec = parse(dm)
            if prec == "day" and plausible(d, end, seen):
                return d, prec, f"p.{p}: {sentence(window, dm.start(), dm.end())[:200]}"
    return None


def file_older_than_seen(f, seen):
    """True when the PDF's own latest date is more than FILE_AGE_DAYS before the day the robot first saw it."""
    dates = []
    for k in ("file_created", "file_modified"):
        try:
            dates.append(dt.date.fromisoformat(str(f.get(k) or "")[:10]))
        except ValueError:
            pass
    return bool(dates) and (seen - max(dates)).days > FILE_AGE_DAYS


def decide(f, pages, seen, filings=None, page_added=None):
    """(tier, date, precision, evidence) for one row, or None. filings: the company's SEC filings when the
    row is a 10-K, 20-F or 40-F (sec_match); page_added: the day its source page started being watched."""
    end = period_end_date(f.get("period_end"), f.get("year"))
    if not re.match(r"^20\d\d-\d\d", str(f.get("period_end") or "")):
        stated = stated_period_end(pages, doc_year(f), seen)
        if stated and (end is None or stated > end):
            end = stated
    m = FILED.search(f.get("Reviewer notes") or "")
    if m:
        d = dt.date.fromisoformat(m.group(1))
        return 1, d, "day", "filing date in the regulator database"
    if filings:
        hit = sec_match(f, end, filings, seen)
        if hit:
            return 1, hit[0], "day", hit[1]
    if is_sec_form(f, pages):
        hit = signed_date(pages, end, seen)
        if hit:
            return (5,) + hit
    hit = find_dates(pages, end, seen, doc_year(f))
    if hit:
        return hit
    m = CAPTURED.search(f.get("Reviewer notes") or "")
    if m:
        cap = dt.date.fromisoformat(m.group(1))
        if end is None or cap > end:
            return 4, cap, "day", "first captured by the Wayback Machine on this day (published on or before it)"
        return None
    if (seen and seen >= FIRST_SEEN_FROM and end and 0 < (seen - end).days <= 548
            and page_added and (seen - page_added).days >= PAGE_WATCHED_DAYS
            and not file_older_than_seen(f, seen)):
        return 4, seen, "day", "first seen by the robot on a watched page (published on or before this day)"
    return None


def eff_type(f):
    return (f.get("source_type_check") or "").strip() or (f.get("source_type") or "").strip()


def rows(token, base, everything=False):
    """The queue: rows never dated, or dated only by first seen / from their first pages, which full text may
    improve. A row whose full text was read without a date is 'not found' and leaves the queue (--all re-reads)."""
    H = {"Authorization": f"Bearer {token}"}
    queue = "" if everything else ", OR({pub_date_source}='', {pub_date_source}='first seen', LEFT({pub_date_evidence}&'', 12)='first pages:')"
    formula = f"AND(NOT({{discard}}), {{year}}>={YEAR_MIN}{queue})"
    params = [("pageSize", "100"), ("filterByFormula", formula)] + [("fields[]", x) for x in FIELDS]
    out, offset = {}, None
    while True:
        p = params + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", f"{API}/{base}/report_library", H, params=p).json()
        for r in j.get("records", []):
            f = r["fields"]
            if f.get("library_id") and eff_type(f) in TYPES and f.get("pub_date_source") != "person":
                out[f["library_id"]] = r
        offset = j.get("offset")
        if not offset:
            return out
        time.sleep(0.2)


def covers(token, base, ids):
    H = {"Authorization": f"Bearer {token}"}
    out, ids = {}, list(ids)
    for i in range(0, len(ids), 50):
        formula = "OR(" + ",".join(f"{{library_id}}='{x}'" for x in ids[i:i + 50]) + ")"
        params = [("pageSize", "100"), ("filterByFormula", formula), ("fields[]", "library_id"), ("fields[]", "cover_text")]
        j = airtable_request("GET", f"{API}/{base}/report_library", H, params=params).json()
        for r in j.get("records", []):
            out[r["fields"].get("library_id")] = r["fields"].get("cover_text") or ""
        time.sleep(0.2)
    return out


def page_added_dates(token, base, page_ids):
    """{monitored_links record id: the day the page was added (its created time)}."""
    H = {"Authorization": f"Bearer {token}"}
    out, ids = {}, sorted(set(page_ids))
    for i in range(0, len(ids), 50):
        formula = "OR(" + ",".join(f"RECORD_ID()='{x}'" for x in ids[i:i + 50]) + ")"
        params = [("pageSize", "100"), ("filterByFormula", formula), ("fields[]", "url")]
        j = airtable_request("GET", f"{API}/{base}/monitored_links", H, params=params).json()
        for r in j.get("records", []):
            out[r["id"]] = dt.date.fromisoformat(r["createdTime"][:10])
        time.sleep(0.2)
    return out


def company_ciks(token, base):
    """{wba_id: sec_cik} for companies with an SEC CIK."""
    H = {"Authorization": f"Bearer {token}"}
    params = [("pageSize", "100"), ("filterByFormula", "AND({sec_cik}!='', {sec_cik}!='none')"),
              ("fields[]", "wba_id"), ("fields[]", "sec_cik")]
    out, offset = {}, None
    while True:
        p = params + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", f"{API}/{base}/companies", H, params=p).json()
        for r in j.get("records", []):
            f = r["fields"]
            if f.get("wba_id") and f.get("sec_cik"):
                out[f["wba_id"]] = str(f["sec_cik"])
        offset = j.get("offset")
        if not offset:
            return out
        time.sleep(0.2)


def sec_annual_filings(cik, sess):
    """[(form, accession, report_date, filing_date)] of the company's recent SEC filings, annual forms first
    in date order as EDGAR lists them (newest first). [] when EDGAR does not answer."""
    from regulator_filings import SEC_UA
    try:
        r = sess.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", headers=SEC_UA, timeout=60)
    except Exception:
        return []
    time.sleep(0.15)                              # SEC fair access: well under 10 requests/second
    if not r.ok:
        return []
    rec = r.json().get("filings", {}).get("recent", {})
    return [(form, acc, rep or "", filed) for form, acc, rep, filed in
            zip(rec.get("form", []), rec.get("accessionNumber", []), rec.get("reportDate", []), rec.get("filingDate", []))]


def read_part(path, wanted):
    out, cur, pages = {}, None, []
    with gzip.open(path, "rt") as fh:
        for line in fh:
            r = json.loads(line)
            if r["id"] not in wanted:
                continue
            if r["id"] != cur:
                if cur is not None:
                    out[cur] = pages
                cur, pages = r["id"], []
            pages.append((r["p"], r.get("t") or ""))
    if cur is not None:
        out[cur] = pages
    return out


def seen_date(f):
    try:
        return dt.date.fromisoformat((f.get("first_seen") or "")[:10])
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--all", action="store_true", help="re-read every key report, not only the queue")
    a = ap.parse_args()
    token, base = os.environ["AIRTABLE_TOKEN"], os.environ["AIRTABLE_BASE"]
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("pub_dates.py")
    lib = rows(token, base, a.all)
    if a.limit:
        lib = dict(list(lib.items())[:a.limit])
    print(f">>> {len(lib):,} key-type reports from {YEAR_MIN}", flush=True)

    import text_core as tc
    store = tc.open_store()
    folder = tempfile.mkdtemp(prefix="pd-")
    names = [n for n in store.assets() if tc.part_of(n)[0]]
    tc.sync_down(store, [n for n in names if n.startswith("docs-")], folder)
    index = tc.load_docs_index(folder, names)
    want = collections.defaultdict(set)
    no_text = []
    for lid in lib:
        d = index.get(lid)
        if d and d.get("status") == "ok":
            want[d["part"]].add(lid)
        else:
            no_text.append(lid)
    texts = {}
    for part, ids in want.items():
        name = f"pages-{part}.jsonl.gz"
        tc.sync_down(store, [name], folder)
        texts.update(read_part(os.path.join(folder, name), ids))
        os.remove(os.path.join(folder, name))
    for lid, cover in covers(token, base, no_text).items():
        texts[lid] = [(0, cover)]          # page 0 = the first pages (cover_text), not the full text
    print(f">>> text: {sum(len(v) for v in want.values()):,} full text, {len(no_text):,} first pages", flush=True)

    added = page_added_dates(token, base, [x for r in lib.values() for x in (r["fields"].get("source_page") or [])])
    ciks = company_ciks(token, base)
    import requests
    sess, edgar = requests.Session(), {}
    sec_rows = 0

    tiers, prec, ups, gaps, samples = collections.Counter(), collections.Counter(), [], [], collections.defaultdict(list)
    changed, change_samples = collections.Counter(), []
    for lid, r in lib.items():
        f = r["fields"]
        seen = seen_date(f)
        pages = texts.get(lid, [])
        filings = None
        wba = f.get("wba_id")
        wba = wba[0] if isinstance(wba, list) and wba else wba
        cik = sec_cik(f, ciks.get(wba or ""))
        if cik and not FILED.search(f.get("Reviewer notes") or "") and is_sec_form(f, pages):
            if cik not in edgar:
                edgar[cik] = sec_annual_filings(cik, sess)
            filings = edgar[cik]
            sec_rows += 1
        page_added = min((added[x] for x in (f.get("source_page") or []) if x in added), default=None)
        res = decide(f, pages, seen, filings, page_added)
        if f.get("Reviewer notes") and FILED.search(f["Reviewer notes"]):
            other = find_dates(texts.get(lid, []), period_end_date(f.get("period_end"), f.get("year")), None, doc_year(f))
            if other and other[0] == 3:
                gaps.append((res[1] - other[1]).days)
        if not res:
            tiers["none"] += 1
            if lid in no_text and f.get("pub_date_source") == "first seen":
                # the first-seen date no longer qualifies and there is no full text to say "not found"
                changed["date removed"] += 1
                if len(change_samples) < 15:
                    change_samples.append(f"{lid}: {f.get('pub_date')} removed (first seen on a page just added)")
                ups.append({"id": r["id"], "fields": {"pub_date_source": None, "pub_date_precision": None,
                                                      "pub_date": None, "pub_date_evidence": None}})
                continue
            if lid not in no_text and f.get("pub_date_source") != "not found":
                if f.get("pub_date"):
                    changed["date removed"] += 1
                    if len(change_samples) < 15:
                        change_samples.append(f"{lid}: {f['pub_date']} removed (was: {(f.get('pub_date_evidence') or '')[:140]})")
                ups.append({"id": r["id"], "fields": {"pub_date_source": "not found", "pub_date_precision": None,
                                                      "pub_date": None, "pub_date_evidence": "full text read, no date found"}})
            continue
        tier, d, p, ev = res
        if ev.startswith("p.0: "):
            ev = "first pages: " + ev[5:]
        tiers[SOURCES[tier]] += 1
        prec[p] += 1
        if len(samples[tier]) < 12:
            samples[tier].append(f"{lid}: {d.isoformat()} ({p}) {ev[:170]}")
        new = {"pub_date": d.isoformat(), "pub_date_precision": p, "pub_date_source": SOURCES[tier], "pub_date_evidence": ev}
        if any((f.get(k) or None) != v for k, v in new.items()):
            ups.append({"id": r["id"], "fields": new})
            if f.get("pub_date") and f["pub_date"] != new["pub_date"]:
                changed["date changed"] += 1
                if len(change_samples) < 15:
                    change_samples.append(f"{lid}: {f['pub_date']} -> {new['pub_date']} ({SOURCES[tier]})")

    n = len(lib) or 1
    md = ["### Publication dates of key reports" + ("" if a.commit else " (dry run)"), "",
          f"Reports {YEAR_MIN}+ of the key types: {len(lib):,}. Dated: {n - tiers['none']:,} ({(n - tiers['none']) / n:.0%}); "
          f"to the day {prec['day']:,}, to the month {prec['month']:,}.", "",
          "| Evidence | Reports |", "| --- | --- |"] + \
         [f"| {SOURCES[t]} | {tiers[SOURCES[t]]:,} |" for t in ORDER] + [f"| none | {tiers['none']:,} |", "",
          f"SEC forms checked against EDGAR: {sec_rows:,} rows of {len(edgar):,} companies.", ""]
    if gaps:
        gaps.sort()
        md += [f"Regulator filings that also state a board approval date ({len(gaps)}): filing minus approval, median "
               f"{gaps[len(gaps) // 2]} days; within 7 days {sum(1 for g in gaps if 0 <= g <= 7) / len(gaps):.0%}, "
               f"within 31 days {sum(1 for g in gaps if 0 <= g <= 31) / len(gaps):.0%}, negative {sum(1 for g in gaps if g < 0)}.", ""]
    for t in (1, 5, 2, 3, 4):
        if samples[t]:
            md += [f"Samples, {SOURCES[t]}:", ""] + [f"- {s}" for s in samples[t]] + [""]
    md += [f"Rows to write: {len(ups):,}. Dates already written that change: {changed['date changed']:,}; "
           f"that are removed (no date found now): {changed['date removed']:,}.", ""]
    if change_samples:
        md += ["Samples of changed dates:", ""] + [f"- {s}" for s in change_samples] + [""]
    open("pub_dates.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    if a.commit:
        H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        for i in range(0, len(ups), 10):
            airtable_request("PATCH", f"{API}/{base}/report_library", H, {"records": ups[i:i + 10], "typecast": True})
            time.sleep(0.22)
    run.finish("ok", rows_in=len(lib), rows_out=len(ups), counters={"tiers": dict(tiers), "precision": dict(prec)})
    run.write_step_summary("ok", dict(tiers), title="Publication dates")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        open("pub_dates.md", "w").write("### Publication dates: the run failed\n\n```\n" + traceback.format_exc()[-3000:] + "\n```\n")
        raise
