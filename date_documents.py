#!/usr/bin/env python3
"""
date_documents.py — give every library document the right doc_year, and say where it came from.

Why: doc_year used to be the largest year anywhere in the address. On sites that file uploads by
date (WordPress: /wp-content/uploads/2022/09/...) that is the month someone uploaded the file, not
the period the document reports on. Absa moved its whole archive in September 2022, so a results
announcement for June 2011 read as 2022. A date in the file name ("..._2025_04_10_AR.pdf") is a
publication date too: an annual report published in April 2025 covers 2024.

What decides the year, strongest first (decide()):
  1. file name    a year in the file name that is not part of a date: "Sustainability-Report-2024.pdf"
  2. cover        a year in the title line at the top of the cover that is not a date
                  ("2026 Remuneration policy"), else a reporting-period phrase on the first pages: "for the year ended 31 December
                  2017", "six months ended 30 June 2011", "Annual Report 2024", "FY2024",
                  "2024年年度报告". Never later than the upload folder or a date in the file name.
  3. file name    a date in the file name: "Board-changes-6-December-2021.pdf" -> 2021
  4. cover        failing all that, the latest date on the cover page ("September 19, 2005"),
                  ignoring maturity dates ("Notes due 28 June 2028") and anything later than the
                  upload folder
  5. address      the upload folder year
  6. none         nothing found: doc_year is left as it was
Every processed row gets doc_year_from (which rule decided it). Rows decided by a date or a folder
before their cover text was read get "waiting for cover" and are looked at again once it is.
Regulator filings (sec.gov, filings.xbrl.org) carry the filing's own fiscal year and are left alone.

Never deletes; only doc_year and doc_year_from are written, and doc_year only when it changes.

Modes: dry run by default (prints the plan, writes a summary for GitHub);
       --commit writes; --limit N caps the rows read per run (0 = all).
Env: AIRTABLE_TOKEN, AIRTABLE_BASE, AIRTABLE_LIBRARY_TABLE (default report_library)
"""
import argparse, collections, datetime, os, re, sys, time, unicodedata
from urllib.parse import quote, unquote, urlsplit

API = "https://api.airtable.com/v0"
F_URL, F_FILE, F_COVER, F_YEAR, F_FROM = "document_url", "file", "cover_text", "doc_year", "doc_year_from"
NO_COVER_YET = "waiting for cover"
FILING_HOSTS = ("sec.gov", "filings.xbrl.org")

YEAR = re.compile(r"(?<!\d)(20[0-3]\d)(?!\d)")
_MON = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
        r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
# a year that is part of a date in a file name: 2025-04-10, 20250410, 10.04.2025, 6-December-2021
# (a bare "2025_12" is NOT a date: it is usually a page range, "report2025_12-13.pdf")
DATE_YEAR = re.compile(r"(?<!\d)(20[0-3]\d)[-_.](?:0?[1-9]|1[0-2])[-_.](?:0?[1-9]|[12]\d|3[01])(?!\d)"
                       r"|(?<!\d)(20[0-3]\d)(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?!\d)"
                       r"|(?<!\d)(?:0?[1-9]|[12]\d|3[01])[-_. ](?:0?[1-9]|1[0-2]|" + _MON + r")[-_. ](20[0-3]\d)(?!\d)"
                       r"|(?<![a-z])" + _MON + r"[-_. ](20[0-3]\d)(?!\d)", re.I)
STRONG = [re.compile(p, re.I) for p in (
    r"(?:year|period|months|quarter|half[- ]year)\s+(?:ended|ending)\s+(?:on\s+)?(?:\d{1,2}(?:st|nd|rd|th)?\s+)?"
    r"(?:" + _MON + r"\s+)?(?:\d{1,2}(?:st|nd|rd|th)?,?\s+)?(20[0-3]\d)",
    r"(?:annual|integrated|sustainability|esg|csr|responsibility|impact|climate|tcfd|financial|interim|"
    r"half[- ]year|non-financial|stewardship|human rights|modern slavery)\s+(?:report|statements?|review|results)"
    r"\s*(?:for\s+(?:the\s+year\s+)?)?(?:fy\s?)?(20[0-3]\d)",
    r"(20[0-3]\d)(?:\s*/\s*\d{2,4})?\s+(?:annual|integrated|sustainability|esg|csr|impact|climate|tcfd)\s+report",
    r"\bfy\s?(20[0-3]\d)\b",
    r"(20[0-3]\d)\s*年\s*(?:度)?\s*(?:年度)?[^\d]{0,12}?(?:报告|報告|보고서)",
    r"(20[0-3]\d)\s*(?:지속가능|사업보고서|통합보고서)",
)]
# a date written out in text ("June 9, 2026", "13 May 2014", "2025-04-10", "2024年3月"): the day a
# release was issued, not what it reports on
TEXT_DATE = re.compile(r"(?:\d{1,2}(?:st|nd|rd|th)?\s+)?" + _MON + r"\.?\s+(?:\d{1,2}(?:st|nd|rd|th)?,?\s+)?20[0-3]\d"
                       r"|\d{1,2}[/.-]\d{1,2}[/.-]20[0-3]\d|20[0-3]\d[/.-]\d{1,2}[/.-]\d{1,2}|20[0-3]\d\s*年\s*\d{1,2}\s*月",
                       re.I)
# years that name something else: a law or standard ("Corporations Act 2001", "ISO 14001:2015") or a
# Japanese fiscal period ending in a month ("2027年3月期" = the year to March 2027, not a date)
NOT_DOC_YEAR = re.compile(r"(?:\bact|\biso\b)[^|;\n]{0,14}?20[0-3]\d|20[0-3]\d\s*年\s*\d{1,2}\s*月\s*期", re.I)
# maturity / due dates are not the document's year ("Notes due 28 June 2028")
DUE = re.compile(r"(?:due|matur[a-z]*|redeem[a-z]*|redemption|expir[a-z]*|until|callable)[^|;\n]{0,30}?20[0-3]\d", re.I)


FY2 = re.compile(r"(?<![a-z])fy[-_ ]?(\d{2})(?!\d)", re.I)     # "SGH-FY22-Results.pdf" -> 2022


def _nfkc(t):
    return unicodedata.normalize("NFKC", t or "")    # full-width digits: "２０２４年" -> "2024年"


def _ok(y, this_year):
    return 2000 <= y <= this_year + 1


def url_parts(url, filename=""):
    """(years in the file name that are not dates, date years in the file name, folder years)."""
    sp = urlsplit((url or "").strip())
    segs = unquote(sp.path).rstrip("/").split("/")
    names = [_nfkc(segs[-1] if segs else ""), _nfkc(unquote(filename or ""))]
    folder = "/".join(segs[:-1]) + "?" + unquote(sp.query)
    plain, dated = set(), set()
    for n in names:
        n = re.sub(r"\.(pdf|htm|html|aspx?)$", "", n, flags=re.I)
        d = {int(y) for m in DATE_YEAR.finditer(n) for y in m.groups() if y}
        dated |= d
        rest = DATE_YEAR.sub(" ", n)
        plain |= {int(y) for y in YEAR.findall(rest)} | {2000 + int(y) for y in FY2.findall(rest)}
    return plain, dated, {int(y) for y in YEAR.findall(folder)}


def decide(url, filename, cover, this_year=None):
    """(year as str or '', rule). See the module docstring for the order."""
    this_year = this_year or datetime.date.today().year
    host = urlsplit(url or "").netloc.lower()
    if any(host == h or host.endswith("." + h) for h in FILING_HOSTS):
        return "", "filing"
    plain, dated, folder = url_parts(url, filename)
    plain = {y for y in plain if _ok(y, this_year)}
    dated = {y for y in dated if _ok(y, this_year)}
    folder = {y for y in folder if _ok(y, this_year)}
    if plain:
        return str(max(plain)), "file name"
    cap = min([this_year + 1] + ([max(folder)] if folder else []) + ([max(dated)] if dated else []))
    cover = _nfkc(cover)
    text = cover[:3000]
    # the title line: a year at the very top of the cover that is not a date ("2026 Remuneration
    # policy", "Annual Report 2024") names the document; later phrases may quote other years
    head = TEXT_DATE.sub(" ", NOT_DOC_YEAR.sub(" ", DUE.sub(" ", text[:100])))
    for m in YEAR.finditer(head):
        y = int(m.group(1))
        if _ok(y, this_year) and y <= cap:
            return str(y), "cover"
    for pat in STRONG:
        for m in pat.finditer(text):
            y = int(m.group(1))
            if _ok(y, this_year) and y <= cap:
                return str(y), "cover"
    if dated:
        return str(max(dated)), "file name" if cover else NO_COVER_YET
    if cover:
        # only dates count here ("September 19, 2005"): a lone year on a cover is as often a law
        # ("Corporations Act 2001") or a standard ("ISO 14001:2015") as the document's year
        ys = [int(y) for d in TEXT_DATE.findall(NOT_DOC_YEAR.sub(" ", DUE.sub(" ", cover[:1000]))) for y in YEAR.findall(d)]
        ys = [y for y in ys if _ok(y, this_year) and y <= cap]
        if ys:
            return str(max(ys)), "cover"
    if folder:
        return str(max(folder)), "address" if cover else NO_COVER_YET
    return "", "none"


def plan_row(rec, this_year=None):
    """(fields to write, (old, new, rule)) for one record."""
    f = rec.get("fields", {})
    att = (f.get(F_FILE) or [{}])[0]
    old = str(f.get(F_YEAR) or "").strip()
    y, rule = decide(f.get(F_URL, ""), att.get("filename", ""), f.get(F_COVER) or "", this_year)
    upd = {F_FROM: rule}
    if y and y != old:
        upd[F_YEAR] = y
    return upd, (old, y or old, rule)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sample", type=int, default=120, help="changes to print in the dry run")
    a = ap.parse_args()
    token, base = os.environ.get("AIRTABLE_TOKEN"), os.environ.get("AIRTABLE_BASE")
    if not (token and base):
        sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
    from monitor_core import airtable_request
    table = os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library")
    url = f"{API}/{base}/{quote(table)}"
    H = {"Authorization": f"Bearer {token}"}
    formula = (f"AND(NOT({{discard}}), OR({{{F_FROM}}}='', "
               f"AND({{{F_FROM}}}='{NO_COVER_YET}', {{{F_COVER}}}!='')))")
    params = [("pageSize", "100"), ("filterByFormula", formula)] + \
             [("fields[]", x) for x in (F_URL, F_FILE, F_COVER, F_YEAR, "library_id")]
    rules, moved, sample, done, offset, wrote = collections.Counter(), collections.Counter(), [], 0, None, 0
    while not a.limit or done < a.limit:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", url, H, params=p).json()
        recs, offset = j.get("records", []), j.get("offset")
        if a.limit:
            recs = recs[:a.limit - done]
        if not recs:
            break
        ups = []
        for r in recs:
            upd, (old, new, rule) = plan_row(r)
            rules[rule] += 1
            if F_YEAR in upd:
                moved[f"{old or 'blank'} -> {new}"] += 1
                if len(sample) < a.sample:
                    sample.append((r["fields"].get("library_id", ""), old, new, rule,
                                   r["fields"].get(F_URL, ""), (r["fields"].get(F_COVER) or "")[:140]))
            ups.append({"id": r["id"], "fields": upd})
        if a.commit:
            for i in range(0, len(ups), 10):
                try:
                    airtable_request("PATCH", url, {**H, "Content-Type": "application/json"},
                                     {"records": ups[i:i + 10], "typecast": True})
                    wrote += len(ups[i:i + 10])
                except Exception as e:
                    print(f">>> WARN: write failed ({e}); rows stay queued.", flush=True)
                time.sleep(0.22)
        done += len(recs)
        if done % 5000 < 100:
            print(f"  {done} rows read", flush=True)
        if not offset:
            break
    changed = sum(moved.values())
    lines = [f"## Document years ({'written' if a.commit else 'dry run, nothing written'})", "",
             f"Rows looked at: {done}. doc_year changed on {changed} ({100 * changed / max(done, 1):.1f}%).", "",
             "Rule that decided: " + ", ".join(f"{k} {v}" for k, v in rules.most_common()), "",
             "Most common changes: " + ", ".join(f"{k} ({v})" for k, v in moved.most_common(15)), "",
             "| library_id | old | new | rule | address | cover starts |", "|---|---|---|---|---|---|"]
    for lid, old, new, rule, u, cov in sample:
        cell = lambda s: str(s).replace("|", "/").replace("\n", " ")
        lines.append(f"| {lid} | {old} | {new} | {rule} | {cell(u)[:110]} | {cell(cov)} |")
    out = "\n".join(lines)
    print(out)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(out + "\n")


if __name__ == "__main__":
    main()
