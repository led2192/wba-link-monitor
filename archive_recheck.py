"""Documents held at an address of the Internet Archive that could not be read: does the archive hold a better capture?

The owner (24 Nov 2026): "this is happening with every file that we try to get from web archive", of a PDF the
browser could not load. Of 958 documents whose address is a capture of web.archive.org, 648 are read and 310 are not,
253 of them "truncated": the capture itself holds a cut file, so neither the robot nor a person's browser will ever
get the document from it. The archive often holds several captures of one address. This script asks the archive's
own index (CDX, open, one request a document, one every 6 seconds, as lanterN-bot; it stops when the index says to slow down three times in a row) which captures of the original
address answered 200 with a PDF, and compares the largest with the one held.

Dry run by default: it counts. The count of 24 Nov 2026: 166 of 310 have a larger capture, 94 are the only PDF the
archive holds of their address, 50 have others and none larger.

With --commit (the owner's yes, the same day) each document with a larger capture gets a new row in report_library
for that capture, as wayback_backfill.py adds its rows: same company, type and year as the row whose capture is cut,
an id made from the new address, so a second run adds nothing. The pipeline then fetches and reads it like any other
row, and the cut one leaves "Documents to fetch" by itself once a document of its company, year and kind is read.
The cut row is not touched, and the 144 with no larger capture are left as they are: the document may still be found
elsewhere.

Prints counts only, never an address or a title.

    python archive_recheck.py [--limit N] [--commit]

Env: SUPABASE_DB_URL; with --commit AIRTABLE_TOKEN and AIRTABLE_BASE; AIRTABLE_QA_TOKEN for the run record (QA_COMMIT).
"""
import argparse, collections, hashlib, os, re, sys, time
from datetime import date

CDX = "https://web.archive.org/cdx/search/cdx"
CAPTURE = re.compile(r"^https?://web\.archive\.org/web/(\d{4,14})[a-z_]*/(.+)$", re.I)
UNREAD = ("no_file", "download_fail", "timeout", "truncated", "too_big", "no_text")
PAUSE = 6.0           # the index answered 429 after 30 requests at one every 1.5 s (first run, 24 Nov 2026)
WAIT_429 = 90         # then wait, ask once more, and stop for good after three refusals in a row
LARGER = 1.05          # a capture counts as larger when its stored length is 5% above the held one's


def split(url):
    """(timestamp, original address) of an address of a capture, or None."""
    m = CAPTURE.match((url or "").strip())
    return (m.group(1), m.group(2)) if m else None


def verdict(held_ts, rows):
    """What the archive's index says of one document. rows: the index's answer, a list of lists whose first is the
    names of the fields (timestamp, mimetype, statuscode, length). One of: 'no capture answered with a PDF',
    'the held capture is the only PDF', 'no larger capture', 'a larger capture exists', 'the held capture is not in the index'."""
    if not rows or len(rows) < 2:
        return "no capture answered with a PDF"
    names = rows[0]
    caps = [dict(zip(names, r)) for r in rows[1:]]
    pdfs = [c for c in caps if str(c.get("statuscode")) == "200" and "pdf" in str(c.get("mimetype") or "").lower()]
    if not pdfs:
        return "no capture answered with a PDF"
    size = lambda c: int(c["length"]) if str(c.get("length") or "").isdigit() else 0
    mine = next((c for c in caps if str(c.get("timestamp")) == held_ts), None)
    best = max(pdfs, key=size)
    if mine is None:
        return "the held capture is not in the index"
    if len(pdfs) == 1 and pdfs[0] is mine:
        return "the held capture is the only PDF"
    if str(best.get("timestamp")) != held_ts and size(best) > size(mine) * LARGER:
        return "a larger capture exists"
    return "no larger capture"


def larger_capture(held_ts, rows):
    """The timestamp of the largest capture that answered with a PDF, when verdict() says a larger one exists; else None."""
    if verdict(held_ts, rows) != "a larger capture exists":
        return None
    names = rows[0]
    pdfs = [c for c in (dict(zip(names, r)) for r in rows[1:]) if str(c.get("statuscode")) == "200" and "pdf" in str(c.get("mimetype") or "").lower()]
    return str(max(pdfs, key=lambda c: int(c["length"]) if str(c.get("length") or "").isdigit() else 0)["timestamp"])


def new_row(old, ts, orig, today=None):
    """The report_library row of a larger capture of the document held as old (library_id, wba_id, company, doc_type,
    year): the same company, type and year, the id made from the new address as wayback_backfill.py makes it."""
    arch = f"https://web.archive.org/web/{ts}id_/{orig}"
    lid, wba, company, doc_type, year = old
    row = {"library_id": f"{wba}-{hashlib.sha1(arch.strip().lower().encode()).hexdigest()[:10]}", "wba_id": wba, "company_name": company, "document_url": arch,
           "match_confidence": "wayback (larger capture)", "first_seen": (today or date.today()).isoformat(),
           "Reviewer notes": f"Added from the Wayback Machine (captured {ts[:4]}-{ts[4:6]}-{ts[6:8]}): a larger capture of the document held as {lid}, whose capture is cut. Original address: {orig}"}
    if doc_type:
        row["source_type"] = doc_type
    if year:
        row["doc_year"] = str(year)
    return row


def wait_of(retry_after):
    """Seconds to wait after a 429: what the index asks for, between 30 and 300; 90 when it does not say."""
    try:
        return min(max(int(str(retry_after).strip()), 30), 300)
    except (TypeError, ValueError):
        return WAIT_429


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
        if not (os.environ.get("AIRTABLE_TOKEN") and os.environ.get("AIRTABLE_BASE")):
            sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE to write.")
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import requests, qa, evidence_db, robot_identity as bot
    run = qa.Run("archive_recheck.py")
    with evidence_db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("select d.library_id, d.url, d.text_status, d.wba_id, d.company, d.doc_type, d.year from public.documents d where d.url like %s and d.text_status = any(%s) order by d.library_id", ("%web.archive.org/web/%", list(UNREAD)))
            docs = cur.fetchall()
        conn.rollback()
    if a.limit:
        docs = docs[:a.limit]
    c, by, add = collections.Counter(), collections.Counter(), []
    sess, refused = requests.Session(), 0
    for n, (lid, url, status, wba, company, doc_type, year) in enumerate(docs, 1):
        got = split(url)
        if not got:
            c["not the address of a capture"] += 1; continue
        ts, orig = got
        if refused >= 3:
            c["not asked: the index said to slow down three times in a row"] += 1; continue
        try:
            ask = lambda: sess.get(CDX, params={"url": orig, "output": "json", "fl": "timestamp,mimetype,statuscode,length", "limit": "200"}, headers={"User-Agent": bot.BOT_UA}, timeout=40)
            r = ask()
            if r.status_code == 429:
                time.sleep(wait_of(r.headers.get("Retry-After"))); r = ask()
            refused = refused + 1 if r.status_code == 429 else 0
            rows = r.json() if r.status_code == 200 and r.text.strip() else []
            v = verdict(ts, rows) if r.status_code == 200 else f"the index answered {r.status_code}"
            better = larger_capture(ts, rows) if r.status_code == 200 else None
            if better and wba:
                add.append(new_row((lid, wba, company, doc_type, year), better, orig))
        except Exception as e:
            v = f"the index did not answer ({type(e).__name__})"
        c[v] += 1; by[f"{status}: {v}"] += 1
        if n % 50 == 0:
            print(f"  {n} of {len(docs)} asked", flush=True)
        time.sleep(PAUSE)
    print(f"### Documents at an address of the Internet Archive that are not read: {len(docs)}\n")
    for k, v in c.most_common():
        print(f"- {v}: {k}")
    print("\nBy the reason the document is not read:")
    for k, v in sorted(by.items()):
        print(f"- {v}: {k}")
    written = 0
    if a.commit and add:
        import wayback_backfill as W
        ids = sorted({r["library_id"] for r in add}); have = set()
        for i in range(0, len(ids), 20):          # a row already added by an earlier run is not added again
            formula = "OR(" + ",".join("{library_id}='%s'" % x for x in ids[i:i + 20]) + ")"
            have |= {x["fields"].get("library_id") for x in W.at_get("report_library", ["library_id"], formula)}
        new = list({r["library_id"]: r for r in add if r["library_id"] not in have}.values()); stats = collections.Counter()
        for i in range(0, len(new), 10):
            created, refused = W.write_rows(new[i:i + 10], stats); written += len(created)
        print(f"\n{written} rows added to the library for a larger capture; {len(have)} were there already; {stats['rows Airtable refused']} refused.", flush=True)
    else:
        print(f"\n{len(add)} rows would be added for a larger capture. Nothing written.", flush=True)
    run.finish("ok", rows_in=len(docs), rows_out=written, counters={re.sub(r"[^a-z0-9]+", "_", k.lower()).strip("_")[:40]: v for k, v in c.items()}, notes="written" if a.commit else "count only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
