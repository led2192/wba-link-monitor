#!/usr/bin/env python3
"""
pub_dates.py: when each key report was published, to the day when the evidence allows, else to the month
(owner's request, 24 Oct 2026).

Scope: annual, integrated, 10-K, sustainability-family reports, registration documents and financial
statements whose year is 2023 or later, not discarded.

Evidence, strongest first (the first that applies decides; all dates must fall after the end of the
period the report covers and no later than the day the robot first saw the document):
    1 regulator filing   the filing date the SEC or ESEF database gives (regulator_filings.py notes it as
                         "filed YYYY-MM-DD" in Reviewer notes): the publication date itself.
    2 publication        the document says so: "Date of publication: 4 March 2025", "Published in March
                         2025", "This report was published on ...", "Release date ...".
    3 board approval     the date the board approved the report or authorised the financial statements
                         for issue: "authorised for issue by the Board of Directors on 27 February 2025",
                         "approved by the Board on ...". Usually the publication day or a few weeks
                         before it; pub_date_source says so, and the measurement on regulator filings
                         (below) says by how much.
    4 first seen         the day the robot first saw the document on a page it was already watching:
                         an upper bound, used only for documents first seen after FIRST_SEEN_FROM (before
                         that date everything was first seen at the initial collection) and within 18
                         months of the period end.
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
FIELDS = ["library_id", "source_type_check", "source_type", "discard", "year", "period_end", "first_seen",
          "Reviewer notes", "pub_date", "pub_date_precision", "pub_date_source", "pub_date_evidence"]
SOURCES = {1: "regulator filing", 2: "publication", 3: "board approval", 4: "first seen"}

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
PUBLISHED = re.compile(r"(?:date\s+of\s+(?:publication|issue)|publication\s+date|release\s+date|published(?:\s+(?:on|in))?|"
                       r"first\s+published|issued\s+on|this\s+report\s+was\s+(?:published|released|issued)(?:\s+(?:on|in))?)"
                       r"\s*[:\-–]?\s*(?:" + DATE + ")", re.I)
APPROVED = re.compile(r"(?:authori[sz]ed\s+for\s+(?:issue|publication)|approved\s+(?:and\s+authori[sz]ed\s+for\s+issue\s+)?"
                      r"(?:for\s+(?:issue|publication)\s+)?by\s+the\s+(?:board|directors|supervisory\s+board|management\s+board)"
                      r"(?:\s+of\s+directors)?)[^.]{0,80}?\b(?:on|dated)\s+(?:" + DATE + ")", re.I)
APPROVED_BEFORE = re.compile(r"\b(?:on\s+)?(?:" + DATE + r"),?\s+the\s+(?:board|directors|supervisory\s+board)[^.]{0,40}?"
                             r"(?:approved|authori[sz]ed)", re.I)
FILED = re.compile(r"\(filed (20\d\d-\d\d-\d\d)\)")
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
    """Last day of the period: '2025-03' -> 2025-03-31; '2025' or a bare year -> 2025-01-01 (only the year is known)."""
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


def find_dates(pages, end, seen):
    """Best (tier, date, precision, evidence) from the text, tiers 2 (publication) and 3 (board approval)."""
    found = {}
    for p, text in pages:
        if not text or not KEYWORDS.search(text):
            continue
        for tier, rx in ((2, PUBLISHED), (3, APPROVED), (3, APPROVED_BEFORE)):
            for m in rx.finditer(text):
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


def decide(f, pages, seen):
    """(tier, date, precision, evidence) for one row, or None."""
    end = period_end_date(f.get("period_end"), f.get("year"))
    m = FILED.search(f.get("Reviewer notes") or "")
    if m:
        d = dt.date.fromisoformat(m.group(1))
        return 1, d, "day", "filing date in the regulator database"
    hit = find_dates(pages, end, seen)
    if hit:
        return hit
    if seen and seen >= FIRST_SEEN_FROM and end and 0 < (seen - end).days <= 548:
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

    tiers, prec, ups, gaps, samples = collections.Counter(), collections.Counter(), [], [], collections.defaultdict(list)
    for lid, r in lib.items():
        f = r["fields"]
        seen = seen_date(f)
        res = decide(f, texts.get(lid, []), seen)
        if f.get("Reviewer notes") and FILED.search(f["Reviewer notes"]):
            other = find_dates(texts.get(lid, []), period_end_date(f.get("period_end"), f.get("year")), None)
            if other and other[0] == 3:
                gaps.append((res[1] - other[1]).days)
        if not res:
            tiers["none"] += 1
            if lid not in no_text and f.get("pub_date_source") != "not found":
                ups.append({"id": r["id"], "fields": {"pub_date_source": "not found", "pub_date_precision": None,
                                                      "pub_date": None, "pub_date_evidence": "full text read, no date found"}})
            continue
        tier, d, p, ev = res
        if ev.startswith("p.0: "):
            ev = "first pages: " + ev[5:]
        tiers[SOURCES[tier]] += 1
        prec[p] += 1
        if len(samples[tier]) < 8:
            samples[tier].append(f"{lid}: {d.isoformat()} ({p}) {ev[:170]}")
        new = {"pub_date": d.isoformat(), "pub_date_precision": p, "pub_date_source": SOURCES[tier], "pub_date_evidence": ev}
        if any((f.get(k) or None) != v for k, v in new.items()):
            ups.append({"id": r["id"], "fields": new})

    n = len(lib) or 1
    md = ["### Publication dates of key reports" + ("" if a.commit else " (dry run)"), "",
          f"Reports {YEAR_MIN}+ of the key types: {len(lib):,}. Dated: {n - tiers['none']:,} ({(n - tiers['none']) / n:.0%}); "
          f"to the day {prec['day']:,}, to the month {prec['month']:,}.", "",
          "| Evidence | Reports |", "| --- | --- |"] + \
         [f"| {SOURCES[t]} | {tiers[SOURCES[t]]:,} |" for t in (1, 2, 3, 4)] + [f"| none | {tiers['none']:,} |", ""]
    if gaps:
        gaps.sort()
        md += [f"Regulator filings that also state a board approval date ({len(gaps)}): filing minus approval, median "
               f"{gaps[len(gaps) // 2]} days; within 7 days {sum(1 for g in gaps if 0 <= g <= 7) / len(gaps):.0%}, "
               f"within 31 days {sum(1 for g in gaps if 0 <= g <= 31) / len(gaps):.0%}, negative {sum(1 for g in gaps if g < 0)}.", ""]
    for t in (2, 3, 4):
        if samples[t]:
            md += [f"Samples, {SOURCES[t]}:", ""] + [f"- {s}" for s in samples[t]] + [""]
    md += [f"Rows to write: {len(ups):,}.", ""]
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
