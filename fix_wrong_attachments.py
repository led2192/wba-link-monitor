#!/usr/bin/env python3
"""
fix_wrong_attachments.py: find documents whose attached "PDF" is really a web page, and send them back to
be fetched again.

Airtable fetches a document's address itself when the robot attaches a file. Some sites answer that request
with a web page instead of the file (their home page, a cookie wall, a "page moved" page) and still say
200, so Airtable stores the page under the PDF's name. On 25 Oct 2026 this was found on the Asian
Development Bank: 79 of its documents held the same 215 KB copy of adb.org's home page, named
adb-financial-report-2025.pdf, adb-sustainability-report-2024.pdf and so on. Build cover text marks them
cover_status = not_pdf, but nothing tried again, because the row looked attached: recover_attach.py only
takes rows with no file, and the office-network runner only rows with file_status = blocked.

This job takes rows that are not discarded, have a file, have cover_status = not_pdf, whose attachment
Airtable typed as a web page (text/html) and whose address looks like a PDF (.pdf in the path). For each it
removes the wrong attachment, sets file_status = blocked (so the office-network runner tries it from an
ordinary connection, with the browser pass), clears cover_status (so the cover is read again once a real
file arrives) and explains it in retry_note. Attachments of other kinds (spreadsheets, Word files, images)
are real files and are left alone; the report counts them.

Runs after Build cover text every night, so a new case is caught the next morning. Dry run by default
(prints counts by attachment type and by site, writes fix_wrong_attachments.md); --commit writes.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE; QA: AIRTABLE_QA_TOKEN (optional).
"""
import argparse, collections, datetime as dt, os, time
from urllib.parse import urlsplit, unquote

import qa
from monitor_core import airtable_request

API = "https://api.airtable.com/v0"
LIB = os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library")
FIELDS = ["library_id", "document_url", "file", "cover_status", "retry_note"]


def looks_like_pdf_address(url):
    path = unquote(urlsplit(url or "").path).lower()
    return ".pdf" in path


def verdict(f):
    """'web_page' (a page stored as the PDF: fix it), 'other_file' (a real non-PDF file: leave it), or None."""
    att = (f.get("file") or [{}])[0]
    kind = (att.get("type") or "").lower()
    if kind.startswith("text/html") or kind == "application/xhtml+xml":
        return "web_page" if looks_like_pdf_address(f.get("document_url")) else "other_file"
    return "other_file"


def note_for(today, size):
    return (f"{today}: the site served a web page ({size / 1024:.0f} KB of HTML) instead of the PDF; the wrong "
            f"attachment was removed and the file goes to the office-network runner")


def rows(token, base):
    H = {"Authorization": f"Bearer {token}"}
    formula = "AND(NOT({discard}), {file}!='', {cover_status}='not_pdf')"
    params = [("pageSize", "100"), ("filterByFormula", formula)] + [("fields[]", x) for x in FIELDS]
    out, offset = [], None
    while True:
        p = params + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", f"{API}/{base}/{LIB}", H, params=p).json()
        out += j.get("records", [])
        offset = j.get("offset")
        if not offset:
            return out
        time.sleep(0.2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    token, base = os.environ["AIRTABLE_TOKEN"], os.environ["AIRTABLE_BASE"]
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("fix_wrong_attachments.py")
    today = dt.date.today().isoformat()
    recs = rows(token, base)
    kinds, hosts, other_kinds, ups = collections.Counter(), collections.Counter(), collections.Counter(), []
    for r in recs:
        f = r["fields"]
        v = verdict(f)
        kinds[v] += 1
        att = (f.get("file") or [{}])[0]
        if v == "web_page":
            hosts[urlsplit(f.get("document_url") or "").netloc.lower()] += 1
            ups.append({"id": r["id"], "fields": {"file": [], "file_status": "blocked", "cover_status": None,
                                                  "retry_note": note_for(today, att.get("size") or 0)}})
        else:
            other_kinds[(att.get("type") or "unknown").lower()] += 1
    md = ["### Attachments that are not the PDF" + ("" if a.commit else " (dry run)"), "",
          f"Documents with a file that is not a PDF: {len(recs):,}. A web page stored as the PDF (fixed): "
          f"{kinds['web_page']:,}; a real file of another kind (left alone): {kinds['other_file']:,}.", "",
          "Sites serving a web page instead of the PDF: " + (", ".join(f"{h} {n:,}" for h, n in hosts.most_common(15)) or "none") + ".", "",
          "Other kinds of file kept: " + (", ".join(f"{k} {n:,}" for k, n in other_kinds.most_common(10)) or "none") + ".", ""]
    open("fix_wrong_attachments.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    if a.commit:
        H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        for i in range(0, len(ups), 10):
            airtable_request("PATCH", f"{API}/{base}/{LIB}", H, {"records": ups[i:i + 10], "typecast": True})
            time.sleep(0.22)
    run.finish("ok", rows_in=len(recs), rows_out=len(ups), counters={"kinds": dict(kinds), "hosts": dict(hosts.most_common(30))})
    run.write_step_summary("ok", dict(kinds), title="Wrong attachments")


if __name__ == "__main__":
    main()
