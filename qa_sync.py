#!/usr/bin/env python3
"""
qa_sync.py — turns the Disclosure Library's own status fields into Pipeline QA failure rows,
takes the robot_* snapshots, captures human corrections as labels, and closes failures that went
silent. Runs nightly after Company coverage; dry run by default.

What it derives (one row per distinct failure, keyed; see qa.py and the design document):
  monitored_links (monitor on, harvested page types only)
    PAGE_BLOCKED      status blocked                          key: registered domain (one row per domain)
    PAGE_GONE         status dead with 404 / 410              key: page URL
    PAGE_UNREACHABLE  status error, or dead with other codes  key: page URL; detail = code or no_response
    PAGE_EMPTY        possible_issue 'No document links found on the page'   detail no_links
    PAGE_UNHARVESTED  unharvested_links >= 3                  message = unharvested_sample
  report_library (discarded rows only feed DOC_DUPLICATE / DOC_WRONG_ROW)
    DOC_FETCH_FAIL    file_status blocked/gone/too_big/not_pdf, or cover_status download_fail
    DOC_UNREADABLE    cover_status no_text / parse_fail / truncated
    TYPE_DISAGREE     type_mismatch = 1 AND the two types satisfy different completeness checks
                      (high_stakes); low-stakes disagreements are counted, not written
    TYPE_MISSING      no effective type 7+ days after creation; detail non_english / no_cover / pending
    DOC_DUPLICATE     discard_reason 'Duplicate of another document'
    DOC_WRONG_ROW     any other discard_reason
    LABEL_CORRECTION  a robot_* snapshot that no longer matches the live value (a person changed it);
                      the same fact is written as a labels row (labelled_by correction)
  companies
    CO_GAP            one row per failed completeness check (gap_pages) and per overdue report
  Document rows are written only for REPORT-SHAPED documents (a report-family type, or a report word
  plus a year of 2023 or later in the address or title); the rest are counted in the run's counters.
  Every code is capped at --max-per-code rows per run; the overflow is counted.

Snapshots (written to the Disclosure Library, --commit only): robot_type, robot_year, robot_title are
filled on rows where they are blank and the live value exists, at most --snapshot-limit rows per run.
Nothing else in the Disclosure Library is ever written by this script.

Modes: dry run by default (prints the counts by code and severity, writes nothing anywhere);
       --commit writes the QA rows (needs AIRTABLE_QA_TOKEN and QA_COMMIT=true) and the snapshots.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE (Disclosure Library), AIRTABLE_QA_TOKEN, AIRTABLE_QA_BASE.
"""
import argparse, collections, datetime as dt, os, re, sys, time
from urllib.parse import quote, urlsplit

import qa
from monitor_core import airtable_request, reg_domain

API = "https://api.airtable.com/v0"
HARVESTED_TYPES = {"reports_hub", "sustainability_page", "sustainability_report", "investor_relations", "policies"}
SUST_FAMILY = {"Sustainability Report", "ESG Report", "CSR Report", "Integrated Report", "Impact Report"}
ANNUAL_FAMILY = {"Annual Report", "Integrated Report", "10K Form"}
REPORT_FAMILIES = SUST_FAMILY | ANNUAL_FAMILY
CHECKS_BY_TYPE = {"Code of Conduct": {"COC"}, "Human Rights Policy": {"HRP"}}
REPORT_WORD = re.compile(r"annual|sustainab|integrated|\besg\b|\bcsr\b|report|memoria|rapport|informe|bericht|"
                         r"relat[oó]rio|disclosure|statement|10-?k|20-?f", re.I)
YEAR_RX = re.compile(r"(?<!\d)(20[2-3]\d)(?!\d)")
MIN_YEAR = 2023
GAP_DETAIL = {"Report hub page": "no_hub", "Sustainability page": "no_sust_page",
              "Sustainability report (recent)": "no_sust_report", "Annual report (recent)": "no_annual_report",
              "Code of Conduct": "no_coc", "Human Rights Policy": "no_hrp"}
OVERDUE_DETAIL = {"Sustainability report": "overdue_sust", "Annual report": "overdue_annual"}
HIGH_GAPS = {"no_sust_report", "no_annual_report", "overdue_sust", "overdue_annual"}
DISCARD_DETAIL = {"Duplicate of another document": ("DOC_DUPLICATE", "discard"),
                  "Wrong company / misattributed": ("DOC_WRONG_ROW", "misattributed"),
                  "Not a disclosure": ("DOC_WRONG_ROW", "not_disclosure"),
                  "Out of language scope": ("DOC_WRONG_ROW", "out_of_scope"),
                  "Out of topic scope": ("DOC_WRONG_ROW", "out_of_scope"),
                  "File unusable": ("DOC_WRONG_ROW", "file_unusable"),
                  "Other": ("DOC_WRONG_ROW", "other")}
FETCH_STATUSES = {"blocked", "gone", "too_big", "not_pdf"}
UNREADABLE = {"no_text", "parse_fail", "truncated"}
PENDING_DAYS = 7


# ---- pure helpers (tested offline) ----------------------------------------------------------------
def checks_of(t):
    """Completeness checks a type satisfies: SUST, ANNUAL, COC, HRP; empty for anything else."""
    out = set()
    if t in SUST_FAMILY:
        out.add("SUST")
    if t in ANNUAL_FAMILY:
        out.add("ANNUAL")
    out |= CHECKS_BY_TYPE.get(t, set())
    return out


def stakes(kw_type, ai_type):
    """high_stakes when picking one type over the other changes a completeness check."""
    return "high_stakes" if checks_of(kw_type) != checks_of(ai_type) else "low_stakes"


def ai_text(v):
    """An aiText field comes back as {'state', 'value', ...}; a plain field as a string."""
    if isinstance(v, dict):
        return (v.get("value") or "").strip()
    return (v or "").strip() if isinstance(v, str) else ""


def year_of(f):
    y = f.get("year")
    if isinstance(y, (int, float)) and 2000 <= int(y) <= 2035:
        return int(y)
    dy = str(f.get("doc_year") or "").strip()
    return int(dy) if dy.isdigit() and 2000 <= int(dy) <= 2035 else None


def eff_type(f):
    return (f.get("source_type_check") or "").strip() or (f.get("source_type") or "").strip()


def report_shaped(f):
    """A report-family type, or a report word AND a 2023+ year in the address or titles."""
    t = eff_type(f)
    y = year_of(f)
    if t in REPORT_FAMILIES and (y is None or y >= MIN_YEAR):
        return True
    text = " ".join([urlsplit(f.get("document_url") or "").path, f.get("auto_title") or "",
                     ai_text(f.get("source_title")), f.get("curated_doc_title") or ""])
    years = [int(m) for m in YEAR_RX.findall(text)]
    if y is not None:
        years.append(y)
    return bool(REPORT_WORD.search(text)) and any(yy >= MIN_YEAR for yy in years)


def doc_severity(f):
    t = eff_type(f)
    y = year_of(f)
    return "High" if (checks_of(t) and (y is None or y >= MIN_YEAR)) else "Medium"


def days_since(iso, today):
    try:
        d = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).date()
        return (today - d).days
    except Exception:
        return None


def derive_page(f):
    """Failures for one monitored_links row -> list of (code, detail, key, severity, fields)."""
    if not f.get("monitor") or (f.get("type") or "").strip().lower() not in HARVESTED_TYPES:
        return []
    url = f.get("url") or ""
    status = (f.get("status") or "").strip().lower()
    code = f.get("http_status")
    code_s = str(int(code)) if isinstance(code, (int, float)) else (str(code or "").strip())
    ctx = {"wba_id": f.get("wba_id"), "company_name": f.get("company_name"), "page_url": url,
           "record_id": f.get("_id"), "http_status": code_s or None, "stage": "monitor"}
    hub = (f.get("type") or "").lower() in ("reports_hub", "sustainability_page", "sustainability_report")
    out = []
    if status == "blocked":
        out.append(("PAGE_BLOCKED", code_s or "challenge", reg_domain(url), "High", ctx))
    elif status == "dead" and code_s in ("404", "410"):
        out.append(("PAGE_GONE", code_s, url, "High" if hub else "Medium", ctx))
    elif status in ("dead", "error"):
        out.append(("PAGE_UNREACHABLE", code_s or "no_response", url, "High" if hub else "Medium", ctx))
    issue = (f.get("possible_issue") or "").strip()
    if issue == "No document links found on the page":
        out.append(("PAGE_EMPTY", "no_links", url, "Medium", dict(ctx, stage="monitor")))
    n = f.get("unharvested_links")
    if isinstance(n, (int, float)) and n >= 3:
        sample = (f.get("unharvested_sample") or "").strip()
        sev = "High" if any(int(y) >= MIN_YEAR for y in YEAR_RX.findall(sample)) else "Medium"
        out.append(("PAGE_UNHARVESTED", str(int(n)), url, sev,
                    dict(ctx, stage="harvest", message=sample[:qa.MESSAGE_CHARS])))
    return out


def derive_doc(f, today):
    """One report_library row -> (failures, labels, snapshot_patch, counters)."""
    failures, labels, patch = [], [], {}
    counts = collections.Counter()
    lid = f.get("library_id") or ""
    ctx = {"wba_id": f.get("wba_id"), "company_name": f.get("company_name"),
           "document_url": f.get("document_url"), "library_id": lid, "record_id": f.get("_id")}
    reason = (f.get("discard_reason") or "").strip()
    if f.get("discard"):
        code, detail = DISCARD_DETAIL.get(reason, ("DOC_WRONG_ROW", "other"))
        failures.append((code, detail, lid, "Low" if code == "DOC_DUPLICATE" else "Medium",
                         dict(ctx, stage="dedupe", message=reason or "discarded with no reason")))
        return failures, labels, patch, counts
    eff = eff_type(f)
    kw, ai = (f.get("source_type") or "").strip(), (f.get("source_type_check") or "").strip()
    shaped = report_shaped(f)
    sev = doc_severity(f)
    fs = (f.get("file_status") or "").strip()
    cs = (f.get("cover_status") or "").strip()
    if fs in FETCH_STATUSES or cs == "download_fail":
        detail = fs if fs in FETCH_STATUSES else "download_fail"
        counts["fetch_fail"] += 1
        if shaped:
            failures.append(("DOC_FETCH_FAIL", detail, lid, sev, dict(ctx, stage="attach")))
        else:
            counts["fetch_fail_not_report"] += 1
    if cs in UNREADABLE:
        counts["unreadable"] += 1
        if shaped:
            failures.append(("DOC_UNREADABLE", cs, lid, sev, dict(ctx, stage="cover")))
        else:
            counts["unreadable_not_report"] += 1
    if str(f.get("type_mismatch") or "").strip() == "1" and kw and ai:
        st = stakes(kw, ai)
        counts[f"disagree_{st}"] += 1
        # a high-stakes disagreement is a row only on a report-shaped document (2023+ or undated):
        # older ones only move the any-year checks and are counted, not written
        if st == "high_stakes" and shaped:
            failures.append(("TYPE_DISAGREE", "high_stakes", lid, "High",
                             dict(ctx, stage="classify", message=f"URL says {kw}; AI says {ai}")))
        elif st == "high_stakes":
            counts["disagree_high_stakes_old"] += 1
    if not eff:
        age = days_since(f.get("created_time"), today)
        if age is not None and age >= PENDING_DAYS:
            lang = (f.get("doc_language") or "").strip()
            detail = "non_english" if lang and lang != "English" else ("no_cover" if cs != "ok" else "pending")
            counts[f"untyped_{detail}"] += 1
            if shaped:
                failures.append(("TYPE_MISSING", detail, lid, "High",
                                 dict(ctx, stage="classify", message=f"language {lang or '?'}; cover {cs or 'not read'}")))
    # snapshots: fill robot_* where blank and the live value exists
    title_live = ai_text(f.get("source_title")) or (f.get("auto_title") or "").strip()
    y = year_of(f)
    if eff and not (f.get("robot_type") or "").strip():
        patch["robot_type"] = eff
    if y and not (f.get("robot_year") or "").strip():
        patch["robot_year"] = str(y)
    if title_live and not (f.get("robot_title") or "").strip():
        patch["robot_title"] = title_live[:250]
    # corrections: a snapshot that no longer matches the live value
    rt = (f.get("robot_type") or "").strip()
    if rt and ai and ai != rt:
        failures.append(("LABEL_CORRECTION", "type", lid, "High",
                         dict(ctx, stage="classify", message=f"robot {rt}; now {ai}")))
        labels.append({"label_id": f"{lid}|type|correction", "target": "document", "field": "type",
                       "stratum": "correction", "company_name": f.get("company_name"), "wba_id": f.get("wba_id"),
                       "doc_label": f.get("doc_label"), "document_url": f.get("document_url"), "library_id": lid,
                       "record_id": f.get("_id"), "doc_language": f.get("doc_language"),
                       "robot_value": rt, "verdict": "Neither", "true_type": ai,
                       "reason": f"changed by hand from {rt} to {ai}" + (f"; URL said {kw}" if kw else ""),
                       "sure": True, "labelled_by": "correction", "labelled_on": today.isoformat(),
                       "reviewed": True, "split": "eval"})
    curated = (f.get("curated_doc_title") or "").strip()
    ry = (f.get("robot_year") or "").strip()
    if curated and ry and y and str(y) != ry:
        failures.append(("LABEL_CORRECTION", "year", lid, "Medium",
                         dict(ctx, stage="classify", message=f"robot {ry}; now {y} (curated title)")))
        labels.append({"label_id": f"{lid}|year|correction", "target": "document", "field": "year",
                       "stratum": "correction", "company_name": f.get("company_name"), "wba_id": f.get("wba_id"),
                       "doc_label": f.get("doc_label"), "document_url": f.get("document_url"), "library_id": lid,
                       "record_id": f.get("_id"), "robot_value": ry, "verdict": "Neither", "true_value": str(y),
                       "sure": True, "labelled_by": "correction", "labelled_on": today.isoformat(),
                       "reviewed": True, "split": "eval"})
    rtitle = (f.get("robot_title") or "").strip()
    if curated and rtitle and curated != rtitle:
        failures.append(("LABEL_CORRECTION", "title", lid, "Medium",
                         dict(ctx, stage="classify", message=f"robot: {rtitle[:120]} | curated: {curated[:120]}")))
        labels.append({"label_id": f"{lid}|title|correction", "target": "document", "field": "title",
                       "stratum": "correction", "company_name": f.get("company_name"), "wba_id": f.get("wba_id"),
                       "doc_label": f.get("doc_label"), "document_url": f.get("document_url"), "library_id": lid,
                       "record_id": f.get("_id"), "robot_value": rtitle[:250], "verdict": "Neither",
                       "true_value": curated[:250], "sure": True, "labelled_by": "correction",
                       "labelled_on": today.isoformat(), "reviewed": True, "split": "eval"})
    return failures, labels, patch, counts


def derive_company(f):
    wba = (f.get("wba_id") or "").strip()
    if not wba:
        return []
    ctx = {"wba_id": wba, "company_name": f.get("company_name"), "record_id": f.get("_id"), "stage": "coverage"}
    out = []
    gaps_text = f.get("gaps") or ""
    for tag in f.get("gap_pages") or []:
        d = GAP_DETAIL.get(tag)
        if not d:
            continue
        line = next((ln for ln in gaps_text.split("\n") if ln.strip()), "")
        out.append(("CO_GAP", d, wba, "High" if d in HIGH_GAPS else "Medium",
                    dict(ctx, message=(f.get("overdue_note") or line or tag)[:qa.MESSAGE_CHARS])))
    for tag in f.get("overdue_reports") or []:
        d = OVERDUE_DETAIL.get(tag)
        if d:
            out.append(("CO_GAP", d, wba, "High", dict(ctx, message=(f.get("overdue_note") or tag)[:qa.MESSAGE_CHARS])))
    return out


# ---- Airtable sweeps ---------------------------------------------------------------------------------
def sweep(token, base, table, fields, formula=None, label=None):
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}"}
    params = [("pageSize", "100")] + [("fields[]", f) for f in fields]
    if formula:
        params.append(("filterByFormula", formula))
    offset = None; n = 0
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", url, headers, params=p).json()
        for rec in j.get("records", []):
            f = rec.get("fields", {}); f["_id"] = rec["id"]
            n += 1
            yield f
        offset = j.get("offset")
        if label and n % 20000 < 100:
            print(f"    {label}: {n:,} rows ...", flush=True)
        time.sleep(0.12)
        if not offset:
            break
    if label:
        print(f"    {label}: {n:,} rows total.", flush=True)


def patch_snapshots(token, base, patches, limit):
    url = f"{API}/{base}/report_library"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    todo = patches[:limit] if limit else patches
    for i in range(0, len(todo), 10):
        airtable_request("PATCH", url, headers, {"records": todo[i:i + 10]})
        time.sleep(0.2)
        if i and i % 2000 == 0:
            print(f"    snapshots: {i:,}/{len(todo):,}", flush=True)
    return len(todo)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write QA rows and robot_* snapshots")
    ap.add_argument("--max-per-code", type=int, default=15000, help="cap rows written per code (overflow counted)")
    ap.add_argument("--snapshot-limit", type=int, default=40000, help="robot_* rows patched per run (0 = all)")
    ap.add_argument("--skip", default="", help="comma-separated sweeps to skip: pages,docs,companies")
    ap.add_argument("--summary-out", default="qa_sync_summary.md", help="markdown summary file (also GITHUB_STEP_SUMMARY)")
    args = ap.parse_args()
    token, base = os.environ.get("AIRTABLE_TOKEN"), os.environ.get("AIRTABLE_BASE")
    if not (token and base):
        sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE (Disclosure Library).")
    if args.commit:
        os.environ["QA_COMMIT"] = "true"
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    today = dt.date.today()
    run = qa.Run("qa_sync.py", trigger=None)
    counters = collections.Counter()
    per_code = collections.Counter()
    blocked = {}                    # domain -> aggregate
    patches = []

    swept = collections.Counter()

    def add(code, detail, key, severity, fields):
        if per_code[code] >= args.max_per_code:
            counters[f"{code}_overflow"] += 1
            return
        if run.failure(code, detail, key=key, severity=severity, **fields):
            per_code[code] += 1

    if "pages" not in skip:
        print(">>> monitored_links ...", flush=True)
        for f in sweep(token, base, "monitored_links",
                       ["url", "wba_id", "company_name", "type", "monitor", "status", "http_status",
                        "possible_issue", "unharvested_links", "unharvested_sample"],
                       "AND({monitor}, OR(" + ",".join(f"{{type}}='{t}'" for t in sorted(HARVESTED_TYPES)) + "))",
                       "monitored_links"):
            swept["pages"] += 1
            for code, detail, key, sev, fields in derive_page(f):
                if code == "PAGE_BLOCKED":
                    b = blocked.setdefault(key, {"n": 0, "codes": collections.Counter(), "wbas": set(), "first": fields})
                    b["n"] += 1; b["codes"][detail] += 1; b["wbas"].add(fields.get("wba_id") or "")
                else:
                    add(code, detail, key, sev, fields)
        for domain, b in blocked.items():
            codes = ", ".join(f"{c} x{n}" for c, n in b["codes"].most_common())
            add("PAGE_BLOCKED", b["codes"].most_common(1)[0][0], domain, "High",
                dict(b["first"], message=f"{b['n']} monitored report pages of {len(b['wbas'])} companies on "
                                         f"{domain} refuse robots ({codes})."))
        counters["blocked_domains"] = len(blocked)

    if "docs" not in skip:
        print(">>> report_library ...", flush=True)
        for f in sweep(token, base, "report_library",
                       ["library_id", "wba_id", "company_name", "document_url", "doc_label", "discard", "discard_reason",
                        "file_status", "cover_status", "source_type", "source_type_check", "type_mismatch",
                        "doc_language", "year", "doc_year", "source_title", "auto_title", "curated_doc_title",
                        "robot_type", "robot_year", "robot_title", "created_time"], None, "report_library"):
            swept["docs"] += 1
            fails, labels, patch, c = derive_doc(f, today)
            counters.update(c)
            for code, detail, key, sev, fields in fails:
                add(code, detail, key, sev, fields)
            for lab in labels:
                run.label(**lab)
            if patch:
                patches.append({"id": f["_id"], "fields": patch})

    if "companies" not in skip:
        print(">>> companies ...", flush=True)
        for f in sweep(token, base, "companies",
                       ["wba_id", "company_name", "gap_pages", "overdue_reports", "gaps", "overdue_note"],
                       None, "companies"):
            swept["companies"] += 1
            for code, detail, key, sev, fields in derive_company(f):
                add(code, detail, key, sev, fields)

    counters["rows_by_code"] = dict(per_code)
    counters["swept"] = dict(swept)
    counters["labels"] = len(run._labels)
    counters["snapshot_candidates"] = len(patches)
    run.finish("ok", rows_in=sum(swept.values()), rows_out=len(run._failures), counters=dict(counters))
    run.close_silent(days=30, script="qa_sync.py")
    # the picture of this run, for the job summary and the pull-request comment
    run.write_step_summary("ok", dict(counters), title="QA sync", path=args.summary_out)

    if patches:
        if args.commit:
            n = patch_snapshots(token, base, patches, args.snapshot_limit)
            print(f">>> snapshots: {n:,} report_library rows got robot_* values "
                  f"({len(patches) - n:,} left for later runs).", flush=True)
        else:
            print(f">>> snapshots: {len(patches):,} report_library rows would get robot_* values "
                  f"(dry run; up to {args.snapshot_limit or 'all'} per run).", flush=True)


if __name__ == "__main__":
    main()
