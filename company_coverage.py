#!/usr/bin/env python3
"""
company_coverage.py — daily per-company coverage summary written into `companies`.

Aggregates monitored_links and report_library by wba_id and writes the numbers onto the
matching `companies` row, so the companies table doubles as the coverage dashboard
("what might be missing"). This replaces the native link+rollup approach on purpose:
populating a link field across ~250k rows via typecast against a table whose primary
field is NOT wba_id is exactly the phantom-row recipe already hit once with SAIL.
This script resolves real record ids and never creates companies rows.

Fields written on `companies` (create them first; see company_coverage.yml header):
    urls_total          -> monitored_links rows for the company
    urls_monitored      -> of those, rows with monitor ticked
    docs_total          -> report_library rows (discarded rows excluded)
    docs_with_file      -> of those, rows with a file attachment
    docs_typed          -> of those, rows with an effective type (not blank / UNKNOWN)
    latest_report_year  -> max {year} over periodic report types
    coverage_breakdown  -> one line per effective type: "Sustainability Report: 14 (latest 2025)"
    coverage_flags      -> semicolon gap flags ("no_reports_hub; no_sust_report_2024+"); blank = healthy;
                           browser_dependent marks zero-doc companies whose pages sit in the
                           browser lane (needs_browser OR dead/error): JS-rendered, CDN-walled or
                           bot-blocked sites. A tech case, not a silent company
    missing_types       -> key source types the company has NO document of, any year:
                           the two report families plus IMPORTANT_EXACT. Analyst-facing:
                           "which important sources are we missing per company"
    pages_breakdown     -> monitored pages per page type: "reports_hub: 2" one per line
    docs_by_year        -> non-discarded docs per resolved year, newest first, plus undated:
                           "2026: 3 | 2025: 12 | 2024: 9 | undated: 7"
    sust_reports_total  -> count of sustainability-family documents held, any year
    sust_years          -> years we hold a sustainability-family document for: "2021; 2023; 2024"
    sust_cadence        -> publication rhythm estimated from sust_years: annual / biennial /
                           ~every Ny / irregular / single / none, with the next expected year
    completeness        -> share of the six COMPLETENESS checks passed (0..1, shown as %)
    gaps                -> the failed checks in plain words, one per line, pages first
    gap_pages           -> the failed checks as tags (multiple select), for filtering
    latest_doc_added    -> date the newest document with a file arrived in the library for this
                           company (the row's creation date), whatever brought it: the robot,
                           add_link or add_file, a regulator filing, the Source Library import
    latest_doc          -> what that document is: year, title, type, how it arrived and its
                           library_id ("2025 | Acea Annual Financial Report (Annual Report); added
                           by hand (file); PT_00015-462193389b")
    coverage_updated    -> date the stats last CHANGED (unchanged rows are not rewritten)

Effective type = source_type_check (AI) when set, else source_type (keyword classifier).
UNKNOWN and blank count as untyped, but UNKNOWN still shows in the breakdown.
Rows with discard ticked are excluded from every count.

Writes are diffed against current values: only rows whose stats changed are PATCHed,
so the daily chained run is quiet and last-modified on companies stays meaningful.
wba_ids present in the data but absent from companies are reported, never created.

Modes:
    (no flag)   dry run: aggregate everything, print totals + gap summary, write nothing
    --commit    write changed rows to companies

Env: AIRTABLE_TOKEN, AIRTABLE_BASE,
     AIRTABLE_LINKS_TABLE     (default monitored_links),
     AIRTABLE_LIBRARY_TABLE   (default report_library),
     AIRTABLE_COMPANIES_TABLE (default companies)
"""
import os, re, time, argparse, collections
from datetime import date
from urllib.parse import quote

from monitor_core import airtable_request

TOKEN = os.environ["AIRTABLE_TOKEN"]
BASE  = os.environ["AIRTABLE_BASE"]
LINKS = os.environ.get("AIRTABLE_LINKS_TABLE", "monitored_links")
LIB   = os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library")
COMP  = os.environ.get("AIRTABLE_COMPANIES_TABLE", "companies")

API = "https://api.airtable.com/v0"
H   = {"Authorization": f"Bearer {TOKEN}"}
WRITE_BATCH = 10

# monitored_links fields
F_WBA = "wba_id"; F_MON = "monitor"; F_PTYPE = "type"; F_NB = "needs_browser"; F_ST = "status"
# report_library fields
F_TYPE_AI = "source_type_check"; F_TYPE_KW = "source_type"; F_YEAR = "year"
F_FILE = "file"; F_DISCARD = "discard"
# companies fields (the 9 stat fields this script owns)
C_WBA = "wba_id"
STAT_FIELDS = ["urls_total", "urls_monitored", "docs_total", "docs_with_file",
               "docs_typed", "latest_report_year", "coverage_breakdown", "coverage_flags",
               "missing_types", "pages_breakdown", "docs_by_year", "sust_years", "sust_cadence",
               "sust_reports_total", "completeness", "gaps", "gap_pages",
               "overdue_reports", "overdue_note", "queue_score", "queue_item", "queue_action",
               "latest_doc_added", "latest_doc", "check_docs", "sust_reporting"]
# written only when a check flips from passing to failing: which document used to pass it and what
# happened to it (discarded, moved, retyped, re-dated, deleted). Newest first, last 10 kept.
C_CHANGE = "completeness_change"
# companies fields this script READS for the queue but never writes (they belong to the person or
# to discover_pages.py)
QUEUE_INPUTS = ["website", "suggested_links", "queue_snooze_until", "completeness_change", "company_name"]
C_UPDATED = "coverage_updated"

# Effective-type families for recency flags. Names match source_type_check options verbatim.
SUST_FAMILY   = {"Sustainability Report", "ESG Report", "CSR Report",
                 "Integrated Report", "Impact Report"}
ANNUAL_FAMILY = {"Annual Report", "Integrated Report", "10K Form"}
# Key source types the organisation expects from every company. The exact set is a
# methodology decision; edit here and the missing_types field re-derives next run.
IMPORTANT_EXACT = ("Code of Conduct", "Human Rights Policy", "Policy Documents")

# Completeness: the six things every company should have. Order = fix order (pages first,
# because documents are harvested from pages). Tag names match the gap_pages options.
COMPLETENESS = ["Report hub page", "Sustainability page", "Sustainability report (recent)",
                "Annual report (recent)", "Code of Conduct", "Human Rights Policy"]

REPORT_TYPES  = SUST_FAMILY | ANNUAL_FAMILY | {
    "Climate Report", "Environmental Reports", "Social Reports", "Interim Reports",
    "Financial Statement", "Registration Document", "CDP Report", "GRI",
    "SASB Index", "Engagement Report"}

YEAR_OK = re.compile(r"^20[0-3][0-9]$")   # sanity guard on the {year} formula output

# Rules version. v1: the checks count by type only. v2 (proposed Oct 2026, measured with --compare
# before it is switched on):
#   annual report check  also passes on a universal registration document (France, Spain, Belgium:
#                        the URD is the annual report), recognised by its title and 100+ pages; the
#                        "Registration Document" type also holds bond prospectuses, final terms and
#                        pricing supplements, which never count
#   sustainability check also passes on a whole annual report, 10-K or URD whose pages hold a
#                        substantive sustainability section (sustainability_class = E embedded):
#                        CSRD sustainability statements, India's BRSR, Hong Kong ESG sections
# v3 (proposed 23 Oct 2026, measured with --compare v2,v3): v2, and the two page checks (report hub,
#                        sustainability page) count only pages that are still watched (monitor ticked)
#                        and have not gone (404 / 410); under v2 an unticked or gone page still passed.
RULES = os.environ.get("COVERAGE_RULES", "v2")       # v2 approved by the owner on 23 Oct 2026
V2_UP = ("v2", "v3")
URD_TITLE = re.compile(r"universal\s+registration|registration\s+document|document\s+d.enregistrement|"
                       r"annual\s+financial\s+report|rapport\s+financier\s+annuel|documento\s+de\s+registro|"
                       r"annual\s+report|rapport\s+annuel", re.I)
DEBT_TITLE = re.compile(r"prospectus|final\s+terms|pricing\s+supplement|supplement|amendment|amendement|offering|programme|"
                        r"program\b|notes\s+due|debt\s+securities|memorandum|bonds?\b", re.I)
PIECES = ("Part of a report", "Summary or short document")
EMBEDDED_TYPES = {"Annual Report", "10K Form", "Registration Document"}


def _name(v):
    return v.get("name") if isinstance(v, dict) else v


def is_urd(f, t):
    """A universal registration document: the annual report in France, Spain, Belgium."""
    if t != "Registration Document":
        return False
    pages = f.get("page_count") or 0
    title = f.get("doc_label") or ""
    return pages >= 100 and bool(URD_TITLE.search(title)) and not DEBT_TITLE.search(title)


BLOCK = re.compile(r"a block of (\d+) sustainability pages", re.I)
STATEMENT_NAMED = re.compile(r"contents names \"[^\"]*(sustainability statement|sustainability report|brsr|"
                             r"business responsibility and sustainability|esg report|non-financial statement)", re.I)
MIN_BLOCK = 8     # owner's decision 23 Oct 2026: a few CSR pages are not reporting sustainability


def is_embedded(f, t):
    """A whole annual report / 10-K / URD whose pages hold a substantive sustainability section: a run
    of 8+ sustainability pages, or a contents entry naming a sustainability statement or BRSR."""
    if t not in EMBEDDED_TYPES or _name(f.get("sustainability_class")) != "E embedded":
        return False
    if _name(f.get("scope")) in PIECES:
        return False
    if t == "Registration Document" and not is_urd(f, t):
        return False
    why = f.get("class_reason") or ""
    m = BLOCK.search(why)
    return bool((m and int(m.group(1)) >= MIN_BLOCK) or STATEMENT_NAMED.search(why))


def origin(f):
    """How a report_library row arrived, from what each writer leaves on it."""
    note = (f.get("Reviewer notes") or "").strip()
    if note.startswith("Uploaded by hand"):
        return "added by hand (file)"
    if note.startswith("From Source Library"):
        return "imported from the Source Library"
    if note.startswith("Added from the"):
        return "regulator filing"
    if (f.get("page_type") or "").strip():
        return f"found by the robot on a {f['page_type'].strip().replace('_', ' ')}"
    return "added by hand (link)"


def latest_doc_text(f, t):
    """'2025 | Annual Report 2025 (Annual Report); found by the robot on a reports hub; PT_1-abc'.
    doc_label is 'year | company | title': the company is dropped, the card is already the company."""
    label = f.get("doc_label")
    parts = [p.strip() for p in (label if isinstance(label, str) else "").split(" | ")]
    if len(parts) >= 3:
        parts = [parts[0]] + parts[2:]
    label = " | ".join(p for p in parts if p) or (f.get("document_url") or "")
    return (f"{label[:180]}" + (f" ({t})" if t else "") + f"; {origin(f)}; {f.get('library_id') or ''}").strip("; ")


def sweep(table, fields, label, formula=None):
    """Yield the fields dict of every row in a table, requesting only what we aggregate."""
    url = f"{API}/{BASE}/{quote(table)}"
    params = [("pageSize", "100")] + [("fields[]", f) for f in fields]
    if formula:
        params.append(("filterByFormula", formula))
    offset = None; n = 0
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", url, H, params=p).json()
        for rec in j.get("records", []):
            n += 1
            yield rec
        offset = j.get("offset")
        if n % 20_000 < 100:
            print(f"    {label}: {n:,} rows swept ...", flush=True)
        time.sleep(0.12)
        if not offset:
            break
    print(f"    {label}: {n:,} rows total.", flush=True)


def eff_type(f):
    """source_type_check first, keyword source_type as fallback. Returns '' when neither helps."""
    t = (f.get(F_TYPE_AI) or "").strip()
    if not t:
        t = (f.get(F_TYPE_KW) or "").strip()
    return t


def wba_formula(wbas):
    """Airtable formula selecting rows of these companies (None = all)."""
    if not wbas:
        return None
    return "OR(" + ",".join("{wba_id}='%s'" % w.replace("'", "") for w in wbas) + ")"


def aggregate(wbas=None):
    """One pass over each big table -> {wba_id: stats dict}."""
    agg = {}

    def co(wba):
        return agg.setdefault(wba, {
            "urls_total": 0, "urls_monitored": 0, "page_types": collections.Counter(), "live_page_types": collections.Counter(), "needs_browser": 0,
            "docs_total": 0, "year_counts": collections.Counter(), "sust_years": set(), "docs_with_file": 0, "docs_typed": 0,
            "type_counts": {}, "type_latest": {}, "latest_report_year": None,
            "ann_years": set(), "blocked": 0, "latest_added": None, "latest_doc": "", "check_doc": {}, "fam_best": {},
            "urd_best": (0, "", ""), "emb_best": (0, "", "")})

    print(">>> sweeping monitored_links ...", flush=True)
    for rec in sweep(LINKS, [F_WBA, F_MON, F_PTYPE, F_NB, F_ST, "http_status"], "monitored_links",
                     wba_formula(wbas)):
        f = rec.get("fields", {})
        wba = (f.get(F_WBA) or "").strip()
        if not wba:
            continue
        c = co(wba)
        c["urls_total"] += 1
        if f.get(F_MON):
            c["urls_monitored"] += 1
        # browser-lane rows: explicitly claimed (needs_browser) OR dead/error, which the
        # browser queue also owns. Counting only needs_browser hid hard bot-walled domains
        # (BMO case, 2026-07-16: every bmo.com row status=error, none flagged needs_browser).
        if f.get(F_NB) or f.get(F_ST) in ("dead", "error", "blocked"):
            c["needs_browser"] += 1
        # 'blocked' is set by the monitors since Oct 2026; older rows still say 'dead' with the code
        if f.get(F_MON) and (f.get(F_ST) == "blocked" or
                             (f.get(F_ST) == "dead" and str(f.get("http_status") or "") in ("401", "403", "429"))):
            c["blocked"] += 1
        pt = (f.get(F_PTYPE) or "").strip()
        if pt:
            c["page_types"][pt] += 1
            gone = f.get(F_ST) == "dead" and str(f.get("http_status") or "").split(".")[0] in ("404", "410")
            if f.get(F_MON) and not gone:
                c["live_page_types"][pt] += 1

    print(">>> sweeping report_library ...", flush=True)
    for rec in sweep(LIB, [F_WBA, F_TYPE_AI, F_TYPE_KW, F_YEAR, F_FILE, F_DISCARD, "created_time", "doc_label",
                           "library_id", "document_url", "page_type", "Reviewer notes", "page_count",
                           "sustainability_class", "scope", "class_reason"], "report_library",
                     wba_formula(wbas)):
        f = rec.get("fields", {})
        wba = (f.get(F_WBA) or "").strip()
        if not wba or f.get(F_DISCARD):
            continue
        c = co(wba)
        c["docs_total"] += 1
        t = eff_type(f)
        if f.get(F_FILE):
            c["docs_with_file"] += 1
            # newest document with a file; ties broken by library_id so the answer is stable
            stamp = ((f.get("created_time") or "")[:19], f.get("library_id") or "")
            if stamp[0] and (c["latest_added"] is None or stamp > c["latest_added"]):
                c["latest_added"] = stamp
                c["latest_doc"] = latest_doc_text(f, t if t != "UNKNOWN" else "")
        if t in ("Code of Conduct", "Human Rights Policy"):
            c["check_doc"].setdefault(t, rec.get("id"))
        if t:
            c["type_counts"][t] = c["type_counts"].get(t, 0) + 1
            if t != "UNKNOWN":
                c["docs_typed"] += 1
        y = f.get(F_YEAR)
        if not (isinstance(y, (int, float)) and YEAR_OK.match(str(int(y)))):
            c["year_counts"]["undated"] += 1
            y = None
        else:
            y = int(y)
            c["year_counts"][y] += 1
            if t in SUST_FAMILY:
                c["sust_years"].add(y)
                if y > c["fam_best"].get("sust", (0, ""))[0]:
                    c["fam_best"]["sust"] = (y, rec.get("id"))
            if t in ANNUAL_FAMILY:
                c["ann_years"].add(y)
                if y > c["fam_best"].get("annual", (0, ""))[0]:
                    c["fam_best"]["annual"] = (y, rec.get("id"))
        if y is not None and is_urd(f, t) and y > c["urd_best"][0]:
            c["urd_best"] = (y, rec.get("id"), f.get("doc_label") or "")
        if y is not None and is_embedded(f, t) and y > c["emb_best"][0]:
            c["emb_best"] = (y, rec.get("id"), f"{f.get('doc_label') or ''} | {f.get('class_reason') or ''}")
        if y is not None:
            if t:
                if y > c["type_latest"].get(t, 0):
                    c["type_latest"][t] = y
                if t in REPORT_TYPES and y > (c["latest_report_year"] or 0):
                    c["latest_report_year"] = y
    return agg


def sust_cadence(years):
    """Publication rhythm of the sustainability family, from the years we actually hold.
    Estimation quality follows library completeness: missing historical documents make a
    rhythm look sparser than it is, so this improves as backfill grows."""
    ys = sorted(years)
    if not ys:
        return "none"
    if len(ys) == 1:
        return f"single ({ys[0]})"
    gaps = [b - a for a, b in zip(ys, ys[1:])]
    modal = collections.Counter(gaps).most_common(1)[0]
    if all(g == 1 for g in gaps):
        return f"annual (next ~{ys[-1] + 1})"
    if all(g == 2 for g in gaps):
        return f"biennial (next ~{ys[-1] + 2})"
    if modal[1] / len(gaps) >= 0.6 and modal[0] > 0:
        return f"~every {modal[0]}y (next ~{ys[-1] + modal[0]})"
    return f"irregular (last {ys[-1]})"


def expected_year(today=None):
    """Latest report year that should be out by now: a year's reports are expected from July of
    the following year (most companies publish within six months of year end)."""
    today = today or date.today()
    return today.year - 1 if today.month >= 7 else today.year - 2


def _step(years):
    ys = sorted(years)
    gaps = [b - a for a, b in zip(ys, ys[1:]) if b > a]
    if not gaps:
        return 1
    g, n = collections.Counter(gaps).most_common(1)[0]
    return g if n / len(gaps) >= 0.6 else 1


def overdue(years, label, target, annual=False):
    """'' or a plain-words note when, by the company's own rhythm, the report for `target` (or
    later) should be out and is not held. Only for companies that published recently (latest held
    within 3 years of target): an older last report reads as stopped or moved, not as overdue."""
    if not years:
        return ""
    last = max(years)
    if last >= target or last < target - 3:
        return ""
    step = 1 if annual else _step(years)
    if last + step > target:
        return ""
    held = ", ".join(str(y) for y in sorted(years)[-4:])
    rhythm = "every year" if step == 1 else f"every {step} years"
    return (f"{label} {last + step} expected: published {rhythm} (held: {held}); "
            f"latest held is {last}.")


def queue(c, out, inputs, year_min, today=None):
    """My queue: one score and one plain-words action per company. The score is what fixing the
    company's problems is worth, dominated by the two disclosures that matter most for 2023-2026
    (sustainability-family and annual report of year_min or later). 0 = nothing for the person:
    the robots handle the rest, or the company is snoozed."""
    today = today or date.today()
    snooze = (inputs.get("queue_snooze_until") or "").strip()
    if snooze and snooze >= today.isoformat():
        return {"queue_score": 0, "queue_item": f"Snoozed until {snooze}.", "queue_action": None}
    failed = set(out["gap_pages"])
    no_sust = "Sustainability report (recent)" in failed
    no_ann = "Annual report (recent)" in failed
    no_hub = "Report hub page" in failed
    over = set(out["overdue_reports"])
    score = (50 if no_sust else 0) + (40 if no_ann else 0)
    score += (10 if "Sustainability report" in over else 0) + (10 if "Annual report" in over else 0)
    score += (10 if no_hub else 0) + (5 if "Sustainability page" in failed else 0)
    score += (8 if "Code of Conduct" in failed else 0) + (8 if "Human Rights Policy" in failed else 0)
    if not score:
        return {"queue_score": 0, "queue_item": "", "queue_action": None}

    def latest(family):
        ys = [c["type_latest"].get(t, 0) for t in family]
        return max(ys) if any(ys) else None
    missing = []
    if no_sust:
        sl = latest(SUST_FAMILY)
        missing.append(f"no {year_min}+ sustainability report ({f'latest held: {sl}' if sl else 'none held'})")
    if no_ann:
        al = latest(ANNUAL_FAMILY)
        missing.append(f"no {year_min}+ annual report ({f'latest held: {al}' if al else 'none held'})")
    what = "; ".join(missing)
    what = (what[0].upper() + what[1:] + ".") if what else ""
    suggested = bool((inputs.get("suggested_links") or "").strip())
    has_site = bool((inputs.get("website") or "").strip()) or c["page_types"].get("homepage", 0) > 0 \
        or c["urls_total"] > 0
    snooze_tip = " If they really have not published it, set queue_snooze_until to a date to hide this until then."

    if not has_site:
        action = "Add the website"
        item = ("New company with no pages yet: paste its main website in the website field. "
                "The robot then searches it tonight and proposes the report pages for you to approve.")
    elif suggested:
        action = "Review suggested pages"
        item = ((what + " ") if what else "") + ("The robot found pages that look like what is missing: check "
                "suggested_links, delete any wrong line, and tick approve_suggestions.")
    elif no_sust or no_ann:
        if c["blocked"] and c["blocked"] >= max(1, c["urls_monitored"] // 2):
            action = "Find the report (site blocks robot)"
            item = (what + " Their website refuses our robot, so only you can get it: download the report "
                    "and drop the PDF in add_file (or paste the PDF link in add_link)." + snooze_tip)
        elif no_hub:
            action = "Find the report page"
            item = (what + " We do not watch their reports page yet: paste the reports/publications page "
                    "link in add_link (or the report PDF itself). The robot is also searching their site.")
        else:
            action = "Find the report"
            item = (what + " Their pages are watched but the report was not found there: if it exists, "
                    "paste the PDF link in add_link (or drop the file in add_file)." + snooze_tip)
    elif no_hub or "Sustainability page" in failed:
        action = "Find the report page"
        item = ("Reports are fine, but we do not watch their " + ("reports page" if no_hub else "sustainability page")
                + ": paste its link in add_link.")
    else:
        pol = [p for p in ("Code of Conduct", "Human Rights Policy") if p in failed]
        action = "Find the policy"
        item = "No " + " and no ".join(pol) + ": if they publish one, paste the PDF link in add_link."
    return {"queue_score": score, "queue_item": item, "queue_action": action}


def compose(c, year_min, rules=None):
    """Stats dict -> the field values to sit on the companies row."""
    lines = []
    for t, n in sorted(c["type_counts"].items(), key=lambda kv: (-kv[1], kv[0])):
        y = c["type_latest"].get(t)
        lines.append(f"{t}: {n} (latest {y})" if y else f"{t}: {n}")
    breakdown = "\n".join(lines)

    rules = rules or RULES

    def family_current(family):
        ok = any(c["type_latest"].get(t, 0) >= year_min for t in family)
        if rules in V2_UP and family is ANNUAL_FAMILY:
            ok = ok or c.get("urd_best", (0,))[0] >= year_min
        if rules in V2_UP and family is SUST_FAMILY:
            ok = ok or c.get("emb_best", (0,))[0] >= year_min
        return ok

    pages_ok = c.get("live_page_types", collections.Counter()) if rules == "v3" else c["page_types"]
    flags = []
    if c["urls_total"] == 0:
        flags.append("no_urls")
    elif c["urls_monitored"] == 0:
        flags.append("nothing_monitored")
    if "reports_hub" not in pages_ok:
        flags.append("no_reports_hub")
    if "sustainability_page" not in pages_ok:
        flags.append("no_sustainability_page")
    if c["docs_total"] == 0:
        flags.append("no_docs")
        if c["needs_browser"] > 0:
            # zero docs on a site the daily monitor can't read = likely a technical wall
            # (JS rendering / CDN-hosted documents), not a company that doesn't publish.
            flags.append("browser_dependent")
    else:
        if c["docs_with_file"] == 0:
            flags.append("no_files")
        if c["docs_typed"] == 0:
            flags.append("nothing_typed")
    if not family_current(SUST_FAMILY):
        flags.append(f"no_sust_report_{year_min}+")
    if not family_current(ANNUAL_FAMILY):
        flags.append(f"no_annual_report_{year_min}+")

    missing = []
    if not any(t in c["type_counts"] for t in SUST_FAMILY):
        missing.append("Sustainability report (any)")
    if not any(t in c["type_counts"] for t in ANNUAL_FAMILY):
        missing.append("Annual report (any)")
    for t in IMPORTANT_EXACT:
        if not c["type_counts"].get(t):
            missing.append(t)

    def latest(family):
        ys = [c["type_latest"].get(t, 0) for t in family]
        return max(ys) if any(ys) else None
    sl, al = latest(SUST_FAMILY), latest(ANNUAL_FAMILY)
    checks = {
        "Report hub page": ("reports_hub" in pages_ok,
                            "No report hub page watched: find the company's reports/publications page and add it."),
        "Sustainability page": ("sustainability_page" in pages_ok,
                                "No sustainability page watched: find the company's sustainability/ESG page and add it."),
        "Sustainability report (recent)": (family_current(SUST_FAMILY),
            f"No sustainability report from {year_min} or later"
            + (f" (latest held: {sl})." if sl else " (none held at all).")),
        "Annual report (recent)": (family_current(ANNUAL_FAMILY),
            f"No annual report from {year_min} or later"
            + (f" (latest held: {al})." if al else " (none held at all).")),
        "Code of Conduct": (bool(c["type_counts"].get("Code of Conduct")), "No Code of Conduct."),
        "Human Rights Policy": (bool(c["type_counts"].get("Human Rights Policy")), "No Human Rights Policy."),
    }
    failed = [k for k in COMPLETENESS if not checks[k][0]]
    completeness = round(1 - len(failed) / len(COMPLETENESS), 4)
    gaps = "\n".join(checks[k][1] for k in failed)
    if failed and c["docs_total"] == 0 and c["needs_browser"] > 0:
        gaps += "\nThe site blocks the normal monitor (browser lane): a technical wall, escalate rather than collect by hand."

    pages = "\n".join(f"{t}: {n}" for t, n in sorted(c["page_types"].items(), key=lambda kv: (-kv[1], kv[0])))
    yc = c["year_counts"]
    year_keys = sorted((k for k in yc if k != "undated"), reverse=True)
    parts = [f"{k}: {yc[k]}" for k in year_keys]
    if yc.get("undated"):
        parts.append(f"undated: {yc['undated']}")
    docs_by_year = " | ".join(parts)
    s_years = "; ".join(str(y) for y in sorted(c["sust_years"]))

    return {"urls_total": c["urls_total"], "urls_monitored": c["urls_monitored"],
            "docs_total": c["docs_total"], "docs_with_file": c["docs_with_file"],
            "docs_typed": c["docs_typed"], "latest_report_year": c["latest_report_year"],
            "coverage_breakdown": breakdown, "coverage_flags": "; ".join(flags),
            "missing_types": "; ".join(missing),
            "sust_reports_total": sum(c["type_counts"].get(t, 0) for t in SUST_FAMILY),
            "pages_breakdown": pages, "docs_by_year": docs_by_year,
            "sust_years": s_years, "sust_cadence": sust_cadence(c["sust_years"]),
            "completeness": completeness, "gaps": gaps, "gap_pages": failed,
            "latest_doc_added": c["latest_added"][0][:10] if c.get("latest_added") else None,
            "latest_doc": c.get("latest_doc") or "",
            "check_docs": check_docs(c, year_min, failed),
            "sust_reporting": sust_reporting(c, year_min),
            **_overdue(c)}


def sust_reporting(c, year_min):
    """dedicated / embedded / both / none, for the recent years (year_min or later)."""
    ded = any(c["type_latest"].get(t, 0) >= year_min for t in SUST_FAMILY)
    emb = c.get("emb_best", (0,))[0] >= year_min
    return "both" if ded and emb else "dedicated" if ded else "embedded" if emb else "none"


def check_docs(c, year_min, failed):
    """'<check>: <record id>' for every passing document check: the document that passes it."""
    docs = {"Code of Conduct": c.get("check_doc", {}).get("Code of Conduct"),
            "Human Rights Policy": c.get("check_doc", {}).get("Human Rights Policy")}
    for check, fam, alt in (("Sustainability report (recent)", "sust", "emb_best"),
                            ("Annual report (recent)", "annual", "urd_best")):
        y, rid = c.get("fam_best", {}).get(fam, (0, ""))
        if y < year_min and RULES in V2_UP:
            y, rid = c.get(alt, (0, ""))[:2]
        docs[check] = rid if y >= year_min else None
    return "\n".join(f"{k}: {v}" for k, v in docs.items() if v and k not in failed) or None


def parse_check_docs(text):
    out = {}
    for line in (text or "").split("\n"):
        k, _, v = line.partition(": ")
        if k and v.startswith("rec"):
            out[k.strip()] = v.strip()
    return out


LOST_TYPES = {"Code of Conduct": {"Code of Conduct"}, "Human Rights Policy": {"Human Rights Policy"},
              "Sustainability report (recent)": SUST_FAMILY, "Annual report (recent)": ANNUAL_FAMILY}


def why_lost(check, wba, f, year_min):
    """Plain words: what happened to the document that used to pass `check` (f = its fields, or None)."""
    if f is None:
        return "the document was deleted"
    label = (f.get("doc_label") or "")[:90]
    if f.get(F_DISCARD):
        r = f.get("discard_reason")
        return f"{label}: discarded ({(r.get('name') if isinstance(r, dict) else r) or 'no reason'})"
    if (f.get(F_WBA) or "").strip() != wba:
        return f"{label}: moved to {f.get(F_WBA) or 'no company'}"
    t = eff_type(f)
    if check == "Sustainability report (recent)" and t in EMBEDDED_TYPES and t not in SUST_FAMILY:
        if not is_embedded(f, t):
            return f"{label}: no longer read as holding a sustainability section ({f.get('class_reason') or 'no class'})"
    elif check == "Annual report (recent)" and t == "Registration Document":
        if not is_urd(f, t):
            return f"{label}: no longer read as a universal registration document"
    elif t not in LOST_TYPES.get(check, set()):
        return f"{label}: type is now {t or 'blank'}"
    y = f.get(F_YEAR)
    if check.endswith("(recent)") and not (isinstance(y, (int, float)) and y >= year_min):
        src = f.get("year_source")
        return f"{label}: year is now {int(y) if isinstance(y, (int, float)) else 'blank'}" + (f" ({src})" if src else "")
    return f"{label}: still looks valid, check by hand"


def explain_losses(losses, year_min):
    """losses: [(wba, check, record id)] -> {(wba, check): reason}. One read per 40 documents."""
    out, ids = {}, sorted({rid for _, _, rid in losses})
    found = {}
    fields = [F_WBA, F_TYPE_AI, F_TYPE_KW, F_YEAR, F_DISCARD, "discard_reason", "doc_label", "year_source",
              "sustainability_class", "scope", "class_reason", "page_count"]
    for i in range(0, len(ids), 40):
        chunk = ids[i:i + 40]
        formula = "OR(" + ",".join(f"RECORD_ID()='{r}'" for r in chunk) + ")"
        for rec in sweep(LIB, fields, "lost documents", formula):
            found[rec["id"]] = rec.get("fields", {})
    for wba, check, rid in losses:
        out[(wba, check)] = why_lost(check, wba, found.get(rid), year_min)
    return out


def _overdue(c):
    target = expected_year()
    notes, tags = [], []
    s = overdue(c["sust_years"], "Sustainability report", target)
    if s:
        tags.append("Sustainability report"); notes.append(s)
    a = overdue(c.get("ann_years") or set(), "Annual report", target, annual=True)
    if a:
        tags.append("Annual report"); notes.append(a)
    return {"overdue_reports": tags, "overdue_note": "\n".join(notes)}


def empty_stats():
    return {"urls_total": 0, "urls_monitored": 0, "page_types": collections.Counter(), "live_page_types": collections.Counter(), "needs_browser": 0,
            "docs_total": 0, "year_counts": collections.Counter(), "sust_years": set(),
            "docs_with_file": 0, "docs_typed": 0, "type_counts": {}, "type_latest": {},
            "latest_report_year": None, "ann_years": set(), "blocked": 0, "latest_added": None, "latest_doc": "", "check_doc": {}, "fam_best": {},
            "urd_best": (0, "", ""), "emb_best": (0, "", "")}


def companies_index(wbas=None):
    """{wba_id: (record_id, current stat field values)} for the diff."""
    out = {}
    for rec in sweep(COMP, [C_WBA] + STAT_FIELDS + QUEUE_INPUTS, "companies", wba_formula(wbas)):
        f = rec.get("fields", {})
        wba = (f.get(C_WBA) or "").strip()
        if wba:
            out[wba] = (rec["id"], f)
    return out


def _norm(v):
    """Blank string, 0-as-float and None all compare stably. PATCHing None clears a field,
    so a target of None vs a stored value correctly counts as a change."""
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        return tuple(sorted(v)) or None
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def changed(current, target):
    """True when any stat field differs from what is already on the row."""
    return any(_norm(current.get(k)) != _norm(target.get(k)) for k in STAT_FIELDS)


DESCRIBE = {
    "v1": "the checks count by type only",
    "v2": "a universal registration document (title + 100 pages) counts as the annual report; a whole annual "
          "report, 10-K or URD with an embedded sustainability section counts for the sustainability check",
    "v3": "v2, and the report hub and sustainability page checks count only pages still watched (monitor "
          "ticked) that have not gone (404/410)",
}


def compare(comp, agg, year_min, out="coverage_compare.md", a="v1", b="v2"):
    """Completeness per company under rules a and b, the checks gained and lost, and the evidence for
    a hand check before b is switched on. Writes nothing to Airtable."""
    import random
    names = {w: (cur.get("company_name") or "") for w, (_, cur) in comp.items()}
    rows = []
    gains, losses = collections.defaultdict(list), collections.defaultdict(list)
    passing = collections.Counter()
    for w in comp:
        st = agg.get(w) or empty_stats()
        x, y = compose(st, year_min, a), compose(st, year_min, b)
        rows.append((x["completeness"], y["completeness"]))
        gx, gy = set(x["gap_pages"] or []), set(y["gap_pages"] or [])
        for check in COMPLETENESS:
            passing[a, check] += check not in gx
            passing[b, check] += check not in gy
            if check in gx and check not in gy:
                key = {"Annual report (recent)": "urd_best", "Sustainability report (recent)": "emb_best"}.get(check)
                why = st[key][2] if key and st.get(key) else ""
                yr = st[key][0] if key and st.get(key) else ""
                gains[check].append((w, names.get(w, ""), yr, why))
            elif check not in gx and check in gy:
                pt = {"Report hub page": "reports_hub", "Sustainability page": "sustainability_page"}.get(check)
                why = (f"{st['page_types'].get(pt, 0)} page(s) of the type, {st.get('live_page_types', {}).get(pt, 0)} "
                       f"watched and not gone") if pt else ""
                losses[check].append((w, names.get(w, ""), "", why))
    n = len(rows) or 1
    va = sum(r[0] for r in rows) / n; vb = sum(r[1] for r in rows) / n
    fa = sum(1 for r in rows if r[0] == 1) / n; fb = sum(1 for r in rows if r[1] == 1) / n
    L = [f"## Completeness: rules {a} vs {b}", "",
         f"{a} = {DESCRIBE.get(a, a)}.", "", f"{b} = {DESCRIBE.get(b, b)}.", "", "Read-only: nothing written.", "",
         f"| | {a} | {b} |", "|---|---:|---:|",
         f"| average completeness | {va:.1%} | {vb:.1%} |", f"| companies at 100% | {fa:.1%} | {fb:.1%} |"]
    for check in COMPLETENESS:
        L.append(f"| companies passing: {check} | {passing[a, check]:,} | {passing[b, check]:,} |")
    L.append("")
    rnd = random.Random(23)
    for label, d in (("gained", gains), ("lost", losses)):
        for check, g in d.items():
            L += [f"### {check}: {len(g)} companies {label} it (sample up to 40)", ""]
            sample = g if len(g) <= 40 else rnd.sample(g, 40)
            L += ["| company | year | evidence |", "|---|---:|---|"]
            L += [f"| {nm[:30]} ({w}) | {y} | {str(why)[:230].replace('|', '/')} |" for w, nm, y, why in sorted(sample, key=lambda x: x[1])]
            L.append("")
    if not gains and not losses:
        L.append("No company changes.")
    text = "\n".join(L)
    print(text, flush=True)
    open(out, "w").write(text + "\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(text + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write changed rows to companies")
    ap.add_argument("--year-min", type=int, default=2024,
                    help="a report family counts as current when its latest year >= this")
    ap.add_argument("--wba", default="", help="only these companies (comma-separated wba_ids); "
                    "used by scan_now.py to refresh one company right after a scan")
    ap.add_argument("--compare", action="store_true",
                    help="read-only: completeness under two rule versions (coverage_compare.md)")
    ap.add_argument("--compare-rules", default="v2,v3", help="the two rule versions to compare, e.g. v2,v3")
    args = ap.parse_args()
    wbas = [w.strip() for w in args.wba.split(",") if w.strip()]

    # companies goes FIRST: it is the cheapest sweep (~2k rows) and the only one whose
    # fields might not exist yet. A missing stat field 422s here in seconds instead of
    # after the ~17-minute aggregation of the two big tables (learned the hard way on
    # the first live run, which died on UNKNOWN_FIELD_NAME at minute 17).
    print(">>> sweeping companies ...", flush=True)
    try:
        comp = companies_index(wbas)
    except Exception:
        print(">>> companies needs these fields (exact names): "
              + ", ".join([C_WBA] + STAT_FIELDS + [C_UPDATED]), flush=True)
        raise

    agg = aggregate(wbas)
    if args.compare:
        ra, rb = (args.compare_rules.split(",") + ["v3"])[:2]
        compare(comp, agg, args.year_min, a=ra.strip(), b=rb.strip())
        return

    orphans = sorted((w for w in agg if w not in comp),
                     key=lambda w: -agg[w]["docs_total"])
    if orphans:
        print(f">>> WARN: {len(orphans)} wba_ids in the data have NO companies row "
              f"(never auto-created). Top by docs: "
              + ", ".join(f"{w}({agg[w]['docs_total']})" for w in orphans[:10]), flush=True)

    today = date.today().isoformat()
    to_write, unchanged, gap_count, losses = [], 0, 0, []
    for wba, (rid, current) in comp.items():
        st = agg.get(wba) or empty_stats()
        target = compose(st, args.year_min)
        target.update(queue(st, target, current, args.year_min))
        if target["coverage_flags"]:
            gap_count += 1
        for check, doc in parse_check_docs(current.get("check_docs")).items():
            if check in target["gap_pages"]:
                losses.append((wba, check, doc))
        if changed(current, target):
            to_write.append({"id": rid, "fields": {**target, C_UPDATED: today}})
        else:
            unchanged += 1

    # a check that passed yesterday and fails today: say which document passed it and what happened
    if losses:
        why = explain_losses(losses, args.year_min)
        by_wba = collections.defaultdict(list)
        for (wba, check), reason in why.items():
            by_wba[wba].append(f"{today} lost {check}: {reason}")
        rows = {r["id"]: r for r in to_write}
        for wba, lines in by_wba.items():
            rid, current = comp[wba]
            old = [ln for ln in (current.get(C_CHANGE) or "").split("\n") if ln.strip()]
            text = "\n".join((lines + old)[:10])
            if rid in rows:
                rows[rid]["fields"][C_CHANGE] = text
            else:
                to_write.append({"id": rid, "fields": {C_CHANGE: text}})
        kinds = collections.Counter(next((k for k in ("deleted", "discarded", "moved", "type is now", "year is now")
                                          if k in r), "still looks valid") for r in why.values())
        report = (f">>> checks lost since the last run: {len(losses)} ("
                  + ", ".join(f"{k} {n}" for k, n in kinds.most_common()) + ")")
        print(report, flush=True)
        for (wba, check), reason in list(why.items())[:40]:
            print(f"    {wba} {check}: {reason}", flush=True)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
                fh.write(report.lstrip("> ") + "\n\n" + "\n".join(f"- {w} {c}: {r}" for (w, c), r in why.items()) + "\n")

    print(f">>> companies: {len(comp):,} | changed: {len(to_write):,} | unchanged: {unchanged:,} "
          f"| with gap flags: {gap_count:,}", flush=True)

    vals = [compose(agg.get(w) or empty_stats(), args.year_min) for w in comp]
    if vals:
        avg = sum(v["completeness"] for v in vals) / len(vals)
        full = sum(1 for v in vals if v["completeness"] == 1) / len(vals)
        miss = collections.Counter(t for v in vals for t in v["gap_pages"])
        print(f">>> completeness: average {avg:.1%} | at 100%: {full:.1%} | missing: "
              + ", ".join(f"{k} {miss[k]}" for k in COMPLETENESS), flush=True)
        od = collections.Counter(t for v in vals for t in v["overdue_reports"])
        print(f">>> overdue (expected by now, not held; target year {expected_year()}): "
              + ", ".join(f"{k} {n}" for k, n in od.items()), flush=True)

    if not args.commit:
        for r in to_write[:5]:
            print(f"    sample -> {r['fields']}", flush=True)
        print(">>> dry run, nothing written. Re-run with --commit.", flush=True)
        return

    url = f"{API}/{BASE}/{quote(COMP)}"
    failed = 0
    for i in range(0, len(to_write), WRITE_BATCH):
        chunk = to_write[i:i + WRITE_BATCH]
        try:
            airtable_request("PATCH", url, H, {"records": chunk})
        except Exception as e:
            failed += len(chunk)
            print(f">>> WARN: batch write failed after retries ({e}); "
                  f"rows stay stale until the next run.", flush=True)
        time.sleep(0.12)
    print(f">>> done. written: {len(to_write) - failed:,} | failed: {failed:,}", flush=True)


if __name__ == "__main__":
    main()
