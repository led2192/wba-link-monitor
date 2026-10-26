#!/usr/bin/env python3
"""
file_dates.py: read the creation and modification dates written inside each key report's PDF.

Every PDF carries dates in its document information (/CreationDate, /ModDate) and often in its XMP
metadata. The creation date is usually the day the final file was exported, days or weeks before the
release; the modification date moves whenever the file is re-saved. Neither is the publication date,
but both are clues: pub_dates.py uses them as its fifth level of evidence ("file metadata") when the
report states no date of its own, and only when they fall after the period end and before the robot
first saw the file (owner's question, 26 Oct 2026: "do you also look at metadata on the files?").

Which rows: the key report types pub_dates.py dates (annual, sustainability, integrated, ESG, CSR,
impact, 10-K, URD, financial statements), dated 2023 or later, not discarded, with an attached file
(or a document_url for files too big to attach), and file_dates_from still empty. Newest first.

Writes file_created, file_modified and file_dates_from ('pdf info', 'xmp', 'none', or why the file
could not be read: 'download_fail', 'not_pdf', 'parse_fail' ...; a transient failure such as
'truncated' or 'download_fail' is left empty so the row is tried again).

Dry run by default (reads --limit files, 40 unless given, and prints what it found); --commit writes.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE; AIRTABLE_QA_TOKEN (optional).
"""
import argparse, collections, datetime as dt, io, os, re, time
from concurrent.futures import ThreadPoolExecutor

import qa
from monitor_core import airtable_request

API = "https://api.airtable.com/v0"
LIB = os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library")
TYPES = {"Sustainability Report", "ESG Report", "CSR Report", "Integrated Report", "Impact Report", "Annual Report",
         "10K Form", "Registration Document", "Financial Statement"}
YEAR_MIN = 2023
FIELDS = ["library_id", "source_type_check", "source_type", "year", "file", "file_status", "document_url"]
TRANSIENT = {"download_fail", "truncated"}
PDF_DATE = re.compile(r"(?:D:)?(\d{4})(\d{2})?(\d{2})?")


def pdf_date(value, today=None):
    """A date from a PDF date string ('D:20250312103000+01'00''), a datetime, or None. Dates before 2000 or
    after today are clock errors, not information."""
    today = today or dt.date.today()
    d = None
    if isinstance(value, dt.datetime):
        d = value.date()
    elif isinstance(value, dt.date):
        d = value
    elif value:
        m = PDF_DATE.match(str(value).strip())
        if m:
            try:
                d = dt.date(int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1))
            except ValueError:
                d = None
    if d and (d.year < 2000 or d > today):
        return None
    return d


def read_dates(data):
    """(created, modified, source) from PDF bytes. Document information first, XMP when it has none."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            pass
    created = modified = None
    try:
        info = reader.metadata or {}
        created, modified = pdf_date(info.get("/CreationDate")), pdf_date(info.get("/ModDate"))
    except Exception:
        info = {}
    if created or modified:
        return created, modified, "pdf info"
    try:
        x = reader.xmp_metadata
        if x is not None:
            created, modified = pdf_date(x.xmp_create_date), pdf_date(x.xmp_modify_date)
    except Exception:
        pass
    if created or modified:
        return created, modified, "xmp"
    return None, None, "none"


def eff_type(f):
    return (f.get("source_type_check") or "").strip() or (f.get("source_type") or "").strip()


def queue(token, base):
    H = {"Authorization": f"Bearer {token}"}
    formula = (f"AND(NOT({{discard}}), {{year}}>={YEAR_MIN}, {{file_dates_from}}='', "
               f"OR({{file}}!='', AND({{file_status}}='too_big', {{document_url}}!='')))")
    params = [("pageSize", "100"), ("filterByFormula", formula), ("sort[0][field]", "year"),
              ("sort[0][direction]", "desc")] + [("fields[]", x) for x in ("library_id", "source_type_check", "source_type")]
    out, offset = [], None
    while True:
        p = params + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", f"{API}/{base}/{LIB}", H, params=p).json()
        out += [r for r in j.get("records", []) if eff_type(r["fields"]) in TYPES]
        offset = j.get("offset")
        if not offset:
            return out
        time.sleep(0.2)


def fresh(token, base, ids):
    """The rows re-read just before use: attachment links expire after about two hours."""
    H = {"Authorization": f"Bearer {token}"}
    formula = "OR(" + ",".join(f"RECORD_ID()='{i}'" for i in ids) + ")"
    params = [("pageSize", "100"), ("filterByFormula", formula)] + [("fields[]", x) for x in FIELDS]
    return airtable_request("GET", f"{API}/{base}/{LIB}", H, params=params).json().get("records", [])


def one(rec):
    import build_cover_text as BCT
    url = BCT.attachment_url(rec)
    if not url:
        return rec, None, None, "no_file"
    kind, data = BCT.download_pdf(url)
    if kind != "ok":
        return rec, None, None, kind
    try:
        c, m, src = read_dates(data)
    except Exception:
        return rec, None, None, "parse_fail"
    return rec, c, m, src


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="files this run (dry run default 40)")
    ap.add_argument("--minutes", type=float, default=300)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    token, base = os.environ["AIRTABLE_TOKEN"], os.environ["AIRTABLE_BASE"]
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    t0 = time.time()
    run = qa.Run("file_dates.py")
    todo = queue(token, base)
    limit = a.limit or (0 if a.commit else 40)
    if limit:
        todo = todo[:limit]
    print(f">>> {len(todo):,} key reports without file dates", flush=True)

    H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    outcome, gaps, samples, done, stopped = collections.Counter(), [], [], 0, ""
    for i in range(0, len(todo), 40):
        if (time.time() - t0) / 60 > a.minutes:
            stopped = f"time budget of {a.minutes:.0f} minutes reached"
            break
        batch = fresh(token, base, [r["id"] for r in todo[i:i + 40]])
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            results = list(ex.map(one, batch))
        ups = []
        for rec, c, m, src in results:
            outcome[src] += 1
            done += 1
            if c and m:
                gaps.append((m - c).days)
            if len(samples) < 12 and src in ("pdf info", "xmp"):
                samples.append(f"{rec['fields'].get('library_id')}: created {c}, modified {m} ({src})")
            if src in TRANSIENT:
                continue
            ups.append({"id": rec["id"], "fields": {"file_created": c.isoformat() if c else None,
                                                    "file_modified": m.isoformat() if m else None,
                                                    "file_dates_from": src}})
        if a.commit:
            for j in range(0, len(ups), 10):
                airtable_request("PATCH", f"{API}/{base}/{LIB}", H, {"records": ups[j:j + 10]})
                time.sleep(0.22)
        if (i // 40) % 25 == 0:
            print(f"    {done:,}/{len(todo):,} read, {(time.time() - t0) / 60:.0f} min", flush=True)

    gaps.sort()
    md = ["### File dates of key reports" + ("" if a.commit else " (dry run)"), "",
          f"Files read: {done:,} of {len(todo):,} waiting. " + ", ".join(f"{k} {v:,}" for k, v in outcome.most_common()) + ".", ""]
    if gaps:
        md += [f"Modified minus created: median {gaps[len(gaps) // 2]} days; same day {sum(1 for g in gaps if g == 0) / len(gaps):.0%}; "
               f"more than 90 days {sum(1 for g in gaps if g > 90) / len(gaps):.0%}.", ""]
    if stopped:
        md += [f"Stopped early: {stopped}. The next run continues.", ""]
    if samples:
        md += ["Samples:", ""] + [f"- {s}" for s in samples] + [""]
    open("file_dates.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    run.finish("ok", rows_in=len(todo), rows_out=done, counters={"outcome": dict(outcome), "stopped": stopped})
    run.write_step_summary("ok", dict(outcome), title="File dates")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        open("file_dates.md", "w").write("### File dates: the run failed\n\n```\n" + traceback.format_exc()[-3000:] + "\n```\n")
        raise
