#!/usr/bin/env python3
"""
recover_attach.py — recover the report_library files Airtable's own URL-fetch could not download.

It downloads each still-missing PDF here (browser User-Agent), validates that it really is a PDF,
and uploads the bytes straight into the attachment cell via Airtable's upload endpoint:

    POST {CONTENT_API}/{base}/{recordId}/{attachmentFieldIdOrName}/uploadAttachment   (hard limit 5 MB/file)

For anything it genuinely cannot attach it writes a terminal `file_status`, so the daily run never
retries a dead row again and the cost stays flat as the library grows. The status field only records
WHY a row has no file; a populated `file` field is the success signal, so a successful upload writes
no status.

  gone      404/410, the document no longer exists
  blocked   401/403, the host refuses automated download
  too_big   a real PDF but over the 5 MB upload limit (and Airtable could not fetch it either)
  not_pdf   200 but the URL serves HTML, not a PDF
Transient timeouts / connection errors are left with an empty status, so they are retried next run.
Since 18 Nov 2026 the report says which failure each was (error_dns, error_timeout, error_tls, error_refused,
error_reset, error_connection, error_redirects, error_bad_url, error_other; http_<code> for another answer of
the site): 1,362 of 1,563 rows were a bare "error" every night and nobody could say why. Counts only; nothing
else changes, these rows are still retried.

Selects rows that are in scope, have no file, and have an empty file_status.
Levers: --types a,b   --year-min YYYY   --limit N (wave)   --commit   --workers N
Without --commit it only reports how many rows it would process (no download, no write).

Env: AIRTABLE_TOKEN, AIRTABLE_BASE, AIRTABLE_LIBRARY_TABLE (default report_library),
     AIRTABLE_ATTACH_FIELD (default file), AIRTABLE_STATUS_FIELD (default file_status)
"""
import argparse, os, sys, time, base64, collections
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urlsplit, unquote, urlparse

from monitor_core import airtable_request   # retrying Airtable helper (reads, writes, uploads)
import requests

API = "https://api.airtable.com/v0"
CONTENT_API = "https://content.airtable.com/v0"   # upload endpoint lives on a different host
F_URL = "document_url"; F_TYPE = "source_type"; F_ID = "library_id"; F_YEAR = "doc_year"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CAP = 5_000_000          # Airtable upload endpoint rejects files above 5 MB
CHUNK = 40               # download this many concurrently, then upload them, then free memory
REASON_TO_STATUS = {"not_found": "gone", "forbidden": "blocked",
                    "too_big": "too_big", "not_pdf": "not_pdf"}


def filename_for(url, lib_id):
    base = unquote(urlsplit(url).path.rsplit("/", 1)[-1].split("?")[0]).strip()
    if not base or "." not in base:
        base = f"{lib_id or 'document'}.pdf"
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    return base[:120]


def select_missing(base, token, table, attach_field, status_field, types, year_min, limit):
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}"}
    fields = [F_URL, F_TYPE, F_ID, F_YEAR, attach_field, status_field]
    params = [("pageSize", "100")] + [("fields[]", f) for f in fields]
    out = []; offset = None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", url, headers, params=p).json()
        for rec in j.get("records", []):
            f = rec.get("fields", {})
            if not f.get(F_URL) or f.get(attach_field) or f.get(status_field):
                continue                                   # no url, already attached, or resolved
            if types and f.get(F_TYPE) not in types:
                continue
            if year_min:
                y = f.get(F_YEAR)
                try:
                    if not y or int(y) < year_min:
                        continue
                except (ValueError, TypeError):
                    continue
            out.append((rec["id"], f[F_URL], filename_for(f[F_URL], f.get(F_ID))))
            if limit and len(out) >= limit:
                return out
        offset = j.get("offset"); time.sleep(0.2)
        if not offset:
            break
    return out


TLS_KINDS = [                       # the first that matches, in this order
    ("error_tls_chain", ("unable to get local issuer", "unable to get issuer", "unable to verify the first")),
    ("error_tls_expired", ("certificate has expired", "not yet valid")),
    ("error_tls_name", ("hostname mismatch", "doesn't match", "does not match", "ip address mismatch")),
    ("error_tls_self_signed", ("self-signed", "self signed")),
    ("error_tls_old", ("handshake failure", "unsafe legacy renegotiation", "wrong version number", "unsupported protocol",
                       "dh key too small", "wrong signature type", "no protocols available", "tlsv1 alert", "sslv3 alert")),
    ("error_tls_cut", ("eof occurred", "unexpected eof", "connection reset")),
]


def tls_kind(text):
    """Which failure of the secure connection, from the error's text. error_tls_chain: the site sends its own
    certificate without the one that signed it, which a browser fetches and this script does not (the only kind
    that can be repaired on our side without trusting less). error_tls_expired, error_tls_name (the certificate is
    of another name), error_tls_self_signed: the site's own fault. error_tls_old: a protocol or cipher no longer
    accepted. error_tls_cut: the site closed during the handshake. error_tls: none of these."""
    text = (text or "").lower()
    for kind, needles in TLS_KINDS:
        if any(n in text for n in needles):
            return kind
    return "error_tls"


def host_of(url):
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def why_failed(e):
    """The kind of a failed request, from its exception: error_dns (the name does not resolve: the site is gone
    or moved), error_timeout, error_tls and its kinds (tls_kind), error_refused, error_reset, error_connection,
    error_redirects, error_bad_url, error_other."""
    ex = requests.exceptions
    text = str(e).lower()
    if isinstance(e, ex.SSLError):
        return tls_kind(text)
    if isinstance(e, ex.Timeout):
        return "error_timeout"
    if isinstance(e, ex.TooManyRedirects):
        return "error_redirects"
    if isinstance(e, (ex.InvalidURL, ex.MissingSchema, ex.InvalidSchema)):
        return "error_bad_url"
    if isinstance(e, ex.ConnectionError):
        if any(w in text for w in ("name or service not known", "nameresolution", "name resolution", "getaddrinfo", "no address associated", "nodename nor servname")):
            return "error_dns"
        if "refused" in text:
            return "error_refused"
        if "timed out" in text:
            return "error_timeout"
        if any(w in text for w in ("reset", "aborted", "remotedisconnected", "incompleteread")):
            return "error_reset"
        return "error_connection"
    return "error_other"


def download(url):
    """Return (bytes, 'ok') or (None, reason)."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=25,
                         stream=True, allow_redirects=True)
        sc = r.status_code
        if sc in (401, 403): r.close(); return None, "forbidden"
        if sc in (404, 410): r.close(); return None, "not_found"
        if sc != 200:        r.close(); return None, f"http_{sc}"
        buf = b""; toobig = False
        for chunk in r.iter_content(65536):
            buf += chunk
            if len(buf) > CAP:
                toobig = True; break
        r.close()
        if toobig:                       return None, "too_big"
        if not buf.startswith(b"%PDF"):  return None, "not_pdf"
        return buf, "ok"
    except Exception as e:
        return None, why_failed(e)


def upload(base, token, attach_field, rid, filename, data):
    url = f"{CONTENT_API}/{base}/{rid}/{quote(attach_field)}/uploadAttachment"
    payload = {"contentType": "application/pdf",
               "file": base64.b64encode(data).decode(),
               "filename": filename}
    try:
        airtable_request("POST", url, {"Authorization": f"Bearer {token}"}, payload, timeout=120)
        return True
    except Exception as e:
        print(f"  upload failed {rid}: {str(e)[:120]}")
        return False


def write_status(base, token, table, status_field, pairs):
    """Set file_status on a batch of rows. pairs = [(record_id, status), ...]."""
    if not pairs:
        return
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}"}
    recs = [{"id": rid, "fields": {status_field: st}} for rid, st in pairs]
    for i in range(0, len(recs), 10):
        airtable_request("PATCH", url, headers,
                         {"records": recs[i:i + 10], "typecast": True})
        time.sleep(0.25)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--types", default="")
    ap.add_argument("--year-min", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("AIRTABLE_TOKEN"); base = os.environ.get("AIRTABLE_BASE")
    table = os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library")
    attach_field = os.environ.get("AIRTABLE_ATTACH_FIELD", "file")
    status_field = os.environ.get("AIRTABLE_STATUS_FIELD", "file_status")
    if not (token and base):
        sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
    types = {t.strip() for t in args.types.split(",") if t.strip()}

    rows = select_missing(base, token, table, attach_field, status_field,
                          types, args.year_min, args.limit)
    print(f"{len(rows)} rows with no file and no status"
          + (f" (types={sorted(types)})" if types else "")
          + (f" (year>={args.year_min})" if args.year_min else ""))
    if not args.commit:
        print("dry run: pass --commit to download, upload and stamp status.")
        return
    if not rows:
        return

    counts = collections.Counter()
    hosts = collections.defaultdict(set)         # kind of failure -> the sites it came from (only their number is printed)
    pending = []
    done = 0
    for i in range(0, len(rows), CHUNK):
        batch = rows[i:i + CHUNK]
        fetched = {}
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(download, r[1]): r for r in batch}
            for fut in as_completed(futs):
                r = futs[fut]
                fetched[r[0]] = (r, *fut.result())
        for rid, (r, data, reason) in fetched.items():
            if data is not None:
                ok = upload(base, token, attach_field, rid, r[2], data)
                counts["attached" if ok else "upload_failed"] += 1
                time.sleep(0.25)
                # success -> file present (no status); failed upload -> leave empty to retry
            else:
                counts[reason] += 1
                hosts[reason].add(host_of(r[1]))
                st = REASON_TO_STATUS.get(reason)        # other/error -> None -> retry next run
                if st:
                    pending.append((rid, st))
            if len(pending) >= 10:
                write_status(base, token, table, status_field, pending); pending = []
        done += len(batch)
        if done % 400 < CHUNK:
            print(f"  {done}/{len(rows)}  attached so far: {counts['attached']}")
    write_status(base, token, table, status_field, pending)

    print("=" * 50)
    first = ["attached", "too_big", "not_found", "forbidden", "not_pdf", "upload_failed"]
    for k in first + sorted(k for k in counts if k not in first):           # then each kind of failure: error_dns, http_503...
        if counts.get(k):
            print(f"  {k:22} {counts[k]:6}" + (f"   from {len(hosts[k])} sites" if hosts.get(k) else ""))
    print("=" * 50)
    print(f"recovered {counts['attached']} files; "
          f"marked {sum(counts[r] for r in REASON_TO_STATUS)} rows with a terminal status.")


if __name__ == "__main__":
    main()
