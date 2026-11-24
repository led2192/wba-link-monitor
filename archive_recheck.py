"""Documents held at an address of the Internet Archive that could not be read: does the archive hold a better capture?

The owner (24 Nov 2026): "this is happening with every file that we try to get from web archive", of a PDF the
browser could not load. Of 958 documents whose address is a capture of web.archive.org, 648 are read and 310 are not,
253 of them "truncated": the capture itself holds a cut file, so neither the robot nor a person's browser will ever
get the document from it. The archive often holds several captures of one address. This script asks the archive's
own index (CDX, open, one request a document, one every 6 seconds, as lanterN-bot; it stops when the index says to slow down three times in a row) which captures of the original
address answered 200 with a PDF, and compares the largest with the one held.

A first step, to know what there is: it counts and writes nothing. What to do with the rows (point them to the better
capture, discard the others with a reason) is decided on these counts.

Prints counts only, never an address or a title.

    python archive_recheck.py [--limit N]

Env: SUPABASE_DB_URL; AIRTABLE_QA_TOKEN for the run record (QA_COMMIT).
"""
import argparse, collections, os, re, sys, time

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


def wait_of(retry_after):
    """Seconds to wait after a 429: what the index asks for, between 30 and 300; 90 when it does not say."""
    try:
        return min(max(int(str(retry_after).strip()), 30), 300)
    except (TypeError, ValueError):
        return WAIT_429


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import requests, qa, evidence_db, robot_identity as bot
    run = qa.Run("archive_recheck.py")
    with evidence_db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("select d.library_id, d.url, d.text_status from public.documents d where d.url like %s and d.text_status = any(%s) order by d.library_id", ("%web.archive.org/web/%", list(UNREAD)))
            docs = cur.fetchall()
        conn.rollback()
    if a.limit:
        docs = docs[:a.limit]
    c, by = collections.Counter(), collections.Counter()
    sess, refused = requests.Session(), 0
    for n, (lid, url, status) in enumerate(docs, 1):
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
            v = verdict(ts, r.json() if r.status_code == 200 and r.text.strip() else []) if r.status_code == 200 else f"the index answered {r.status_code}"
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
    print("\nNothing written.", flush=True)
    run.finish("ok", rows_in=len(docs), rows_out=0, counters={re.sub(r"[^a-z0-9]+", "_", k.lower()).strip("_")[:40]: v for k, v in c.items()}, notes="count only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
