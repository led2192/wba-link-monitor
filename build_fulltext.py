#!/usr/bin/env python3
"""
build_fulltext.py — full text of the attached PDFs, page by page, into the text store.

cover_text holds the first six pages of a document, capped at 10,000 characters. That is enough
to type a document and not enough to answer "which companies mention X": a benchmark reference
usually sits in a ratings table on page 40. This script reads EVERY page of every attached PDF
in scope and writes the text to the store described in text_core.py. mention_search.py then
scans that store.

Nothing is written to Airtable. The record of what has been extracted lives in the store itself
(the docs-<part> files), next to the text it describes, so the two cannot drift apart and
report_library needs no new field. The queue is "in scope, has a file, not yet in the store".

Scope (which documents are worth reading in full):
    default      the report and policy families: sustainability, ESG, CSR, integrated, impact and
                 social reports, annual reports, engagement, climate and environmental reports,
                 policies, codes of conduct, human rights policies, GRI / SASB / CDP documents and
                 UNKNOWN; year >= --year-min, plus undated documents.
                 Left out on purpose: interim reports, statutory documents, presentations,
                 financial statements, 10-K forms, registration documents, certifications. They
                 are over half of all pages and the least likely home of a benchmark reference.
    --types      an explicit comma-separated list of types, or "all" for every attached PDF.
The effective type is source_type_check (AI) when set, else source_type (keyword), the same rule
company_coverage.py uses. Discarded rows are never read.

Outcome per document (the `status` in the docs file):
    ok           text extracted
    no_text      the PDF has no text layer (scanned). Not searchable; this is a coverage gap,
                 not evidence that the document does not mention something
    encrypted    needs a password
    parse_fail   complete file, pypdf cannot read it
    timeout      took longer than its time allowance (240 s, more for documents over 150 pages)
    too_big      over MAX_MB
    not_pdf      the attachment is not a PDF
    crash        killed the worker process (isolated and skipped)
    no_file      the row or its attachment disappeared between planning and reading
    truncated / download_fail    transient; retried on later runs, at most 3 times

Modes:
    (no flag)    dry run: plan, print what is in scope / in the store / queued, extract a small
                 sample and print what came out. Writes nothing.
    --commit     extract the queue into the store
    --plan       planning job of the workflow: with --commit, upload the metadata snapshot and
                 the queue to the store and hand the shard list to the next job
    --shard i/N --queue NAME    one extraction shard of a planned queue
    --retry-failed              put parse_fail / timeout / crash documents back in the queue
                                (after a pypdf upgrade, for instance)

Safe to interrupt and to re-run: parts are append-only, a part is uploaded before it is counted,
and a shard stops by itself before the job limit (--budget-min) with everything flushed.

Env: AIRTABLE_TOKEN, AIRTABLE_BASE, AIRTABLE_LIBRARY_TABLE (default report_library),
     TEXT_STORE_REPO + TEXT_STORE_TOKEN (private GitHub repository), or TEXT_STORE_DIR (folder),
     FULLTEXT_DOC_TIMEOUT (default 240 s), FULLTEXT_MAX_MB (default 150)
"""
import os, io, sys, json, gzip, time, math, signal, hashlib, logging, argparse, collections, tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from urllib.parse import quote

import requests

import text_core as tc
from text_core import clean_page_text, TERMINAL, MAX_ATTEMPTS

API = "https://api.airtable.com/v0"
LIB = os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library")

# report_library fields
F_ID = "library_id"; F_WBA = "wba_id"; F_NAME = "company_name"; F_FILE = "file"; F_DISCARD = "discard"
F_TYPE_AI = "source_type_check"; F_TYPE_KW = "source_type"; F_YEAR = "year"; F_LANG = "doc_language"
F_PAGES = "page_count"; F_LABEL = "doc_label"; F_URL = "document_url"
SWEEP_FIELDS = [F_ID, F_WBA, F_NAME, F_FILE, F_TYPE_AI, F_TYPE_KW, F_YEAR, F_LANG, F_PAGES, F_LABEL, F_URL]

# Types read in full by default. Names match the source_type_check options verbatim.
CORE_TYPES = [
    "Sustainability Report", "ESG Report", "CSR Report", "Integrated Report", "Impact Report",
    "Social Reports", "Annual Report", "Engagement Report", "Climate Report", "Environmental Reports",
    "Policy Documents", "Code of Conduct", "Human Rights Policy", "GRI", "SASB Index", "CDP Report",
    "UNKNOWN",
    # annual filings that can hold a sustainability section (France's universal registration
    # documents carry the CSRD statement; 10-Ks a human capital section; financial statements volumes
    # are often whole statutory reports): read in full so sustainability_class.py can judge them
    "10K Form", "Registration Document", "Financial Statement",
]

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
DL_TIMEOUT = 90
DL_TRIES = 3
MAX_MB = int(os.environ.get("FULLTEXT_MAX_MB", "150"))            # skip files above this size
MAX_PAGES = 3000             # read at most this many pages of one document
MIN_CHARS = 200              # less text than this over a whole document = no text layer
DOC_TIMEOUT = int(os.environ.get("FULLTEXT_DOC_TIMEOUT", "240"))  # seconds per document, download included
BATCH = 32                   # documents per batch; attachment URLs are fetched fresh per batch
FLUSH_DOCS = 300             # flush a part after this many documents ...
FLUSH_CHARS = 120_000_000    # ... or this much text ...
FLUSH_SECONDS = 20 * 60      # ... or this long, whichever comes first
PAGES_PER_SHARD_HOUR = 100_000   # planning figure, deliberately low (measured: ~16 pages/s per core)
SHARD_HOURS = 4

logging.getLogger("pypdf").setLevel(logging.ERROR)   # pypdf narrates every repaired object


# ----------------------------------------------------------------------------------------------
# Airtable (read only)
# ----------------------------------------------------------------------------------------------
def _airtable():
    from monitor_core import airtable_request      # the retrying helper every script shares
    token = os.environ.get("AIRTABLE_TOKEN"); base = os.environ.get("AIRTABLE_BASE")
    if not (token and base):
        sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
    return airtable_request, f"{API}/{base}/{quote(LIB)}", {"Authorization": f"Bearer {token}"}


def eff_type(f):
    """source_type_check first, keyword source_type as fallback (same rule as company_coverage)."""
    return (f.get(F_TYPE_AI) or "").strip() or (f.get(F_TYPE_KW) or "").strip()


def year_of(f):
    y = f.get(F_YEAR)
    try:
        y = int(float(y))
    except (TypeError, ValueError):
        return None
    return y if 1990 <= y <= 2100 else None


def pick_attachment(cell, want=None):
    """The PDF among a row's attachments: the one planned (by id) when it is still there, else
    the first that looks like a PDF, else the first."""
    cell = cell or []
    if want:
        for a in cell:
            if a.get("id") == want:
                return a
    for a in cell:
        if (a.get("type") or "").lower() == "application/pdf" or (a.get("filename") or "").lower().endswith(".pdf"):
            return a
    return cell[0] if cell else None


def row_doc(rec):
    """A report_library record -> the dict this script and the metadata snapshot use, or None
    when the row has no library_id or no attachment."""
    f = rec.get("fields", {})
    lid = (f.get(F_ID) or "").strip()
    att = pick_attachment(f.get(F_FILE))
    if not lid or not att:
        return None
    y = year_of(f)
    return {"id": lid, "rec": rec["id"], "wba": (f.get(F_WBA) or "").strip(), "co": f.get(F_NAME) or "",
            "type": eff_type(f), "year": y if y is not None else "", "lang": f.get(F_LANG) or "",
            "label": f.get(F_LABEL) or "", "url": f.get(F_URL) or "", "pages": int(f.get(F_PAGES) or 0),
            "att": att.get("id") or "", "size": int(att.get("size") or 0)}


def refresh_rows(rec_ids):
    """Current state of specific rows, read by record id: the documents that are in the store but
    were not returned by this run's sweep (extracted earlier under a wider scope, or since
    discarded, detached or deleted). Rows that are gone or discarded simply do not come back."""
    request, url, headers = _airtable()
    out = []
    for i in range(0, len(rec_ids), 100):
        chunk = rec_ids[i:i + 100]
        formula = "OR(" + ",".join(f"RECORD_ID()='{r}'" for r in chunk) + ")"
        params = [("pageSize", "100"), ("filterByFormula", formula)]
        params += [("fields[]", f) for f in SWEEP_FIELDS + [F_DISCARD]]
        for rec in request("GET", url, headers, params=params).json().get("records", []):
            if rec.get("fields", {}).get(F_DISCARD):
                continue
            d = row_doc(rec)
            if d:
                out.append(d)
        time.sleep(0.12)
    return out


def sweep_scope(types, year_min, keep_undated):
    """Every non-discarded row with a file that falls in scope -> list of dicts (one per document).
    The type filter runs server-side (it removes most of the 330k rows); the effective-type and
    year rules are applied here, where they can be read."""
    request, url, headers = _airtable()
    conds = [f"{{{F_FILE}}}", f"NOT({{{F_DISCARD}}})"]
    if types:
        ors = ["{%s}='%s'" % (fld, t.replace("'", "\\'")) for t in types for fld in (F_TYPE_AI, F_TYPE_KW)]
        conds.append("OR(" + ",".join(ors) + ")")
    params = [("pageSize", "100"), ("filterByFormula", "AND(" + ",".join(conds) + ")")]
    params += [("fields[]", f) for f in SWEEP_FIELDS]
    wanted = set(types or [])
    out = []; offset = None; n = 0
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = request("GET", url, headers, params=p).json()
        for rec in j.get("records", []):
            n += 1
            d = row_doc(rec)
            if d is None or (wanted and d["type"] not in wanted):
                continue
            if d["year"] == "":
                if not keep_undated:
                    continue
            elif year_min and d["year"] < year_min:
                continue
            out.append(d)
        offset = j.get("offset")
        if not offset:
            break
        if n % 10_000 < 100:
            print(f"    report_library: {n:,} rows read ...", flush=True)
        time.sleep(0.12)
    print(f"    report_library: {n:,} rows read, {len(out):,} documents in scope.", flush=True)
    return out


def fresh_attachments(rec_ids):
    """{record id: attachment cell} read just before a batch is downloaded. Attachment URLs
    expire after a couple of hours, so the ones seen at planning time cannot be reused."""
    request, url, headers = _airtable()
    formula = "OR(" + ",".join(f"RECORD_ID()='{r}'" for r in rec_ids) + ")"
    params = [("pageSize", "100"), ("filterByFormula", formula), ("fields[]", F_FILE)]
    j = request("GET", url, headers, params=params).json()
    return {r["id"]: r.get("fields", {}).get(F_FILE) or [] for r in j.get("records", [])}


# ----------------------------------------------------------------------------------------------
# One document (runs in a worker process)
# ----------------------------------------------------------------------------------------------
class _Timeout(BaseException):
    """Raised by the per-document timer. A BaseException on purpose: pypdf and requests both
    contain broad `except Exception` blocks, and a timeout they swallow is no timeout at all."""


def _alarm(signum, frame):
    raise _Timeout()


def fetch_pdf(url, max_bytes):
    """(kind, data). Same completeness rule as build_cover_text.download_pdf: under load the
    attachment CDN can answer 200 with a body cut short, which starts with %PDF and lacks the
    trailing %%EOF. Such a body is retried and, failing that, reported as 'truncated' (transient),
    never handed to pypdf, whose recovery scan on a broken file is very slow."""
    last = "download_fail"; delay = 1.0
    for _ in range(DL_TRIES):
        try:
            with requests.get(url, headers=UA, timeout=DL_TIMEOUT, stream=True) as resp:
                resp.raise_for_status()
                clen = resp.headers.get("Content-Length")
                if clen and clen.isdigit() and int(clen) > max_bytes:
                    return "too_big", b""
                buf = io.BytesIO()
                for chunk in resp.iter_content(1 << 18):
                    buf.write(chunk)
                    if buf.tell() > max_bytes:
                        return "too_big", b""
                data = buf.getvalue()
        except _Timeout:
            raise
        except Exception:
            last = "download_fail"; time.sleep(delay); delay *= 2; continue
        if b"%PDF" not in data[:1024]:
            return "not_pdf", b""
        short = bool(clen and clen.isdigit() and len(data) < int(clen))
        if short or b"%%EOF" not in data[-2048:]:
            last = "truncated"; time.sleep(delay); delay *= 2; continue
        return "ok", data
    return last, b""


def pdf_pages(data, max_pages=MAX_PAGES):
    """(status, [(page_no, text)], page_count). Pages with no text are left out; page numbers
    are the PDF's own, starting at 1, so a hit can be opened at the right page."""
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            ok = 0
            try:
                ok = int(reader.decrypt(""))          # most "encrypted" PDFs have an empty password
            except Exception:
                ok = 0
            if not ok:
                return "encrypted", [], None
        n = len(reader.pages)
    except _Timeout:
        raise
    except Exception:
        return "parse_fail", [], None
    pages = []; chars = 0
    for i in range(min(n, max_pages)):
        try:
            t = clean_page_text(reader.pages[i].extract_text() or "")
        except _Timeout:
            raise
        except Exception:
            t = ""                                    # one unreadable page must not lose the document
        if t:
            pages.append((i + 1, t)); chars += len(t)
    if chars < MIN_CHARS:
        return "no_text", [], n
    return "ok", pages, n


def extract_one(task):
    """task: {"id", "rec", "att", "url"} -> {"doc": docs-file line, "pages": [(n, text)]}."""
    t0 = time.time()
    doc = {"id": task["id"], "rec": task["rec"], "att": task.get("att") or "", "status": "",
           "pages": 0, "text_pages": 0, "chars": 0, "secs": 0.0, "at": time.strftime("%Y-%m-%d", time.gmtime())}
    pages = []
    timed = hasattr(signal, "SIGALRM")               # not on Windows: no per-document timeout there
    if timed:
        signal.signal(signal.SIGALRM, _alarm)
        # fires at the limit and then every 5 s, in case the first one lands inside a C call
        signal.setitimer(signal.ITIMER_REAL, int(task.get("timeout") or DOC_TIMEOUT), 5)
    try:
        if not task.get("url"):
            doc["status"] = "no_file"
        else:
            kind, data = fetch_pdf(task["url"], int(task.get("max_mb") or MAX_MB) * 1024 * 1024)
            if kind != "ok":
                doc["status"] = kind
            else:
                status, pages, n = pdf_pages(data)
                doc["status"] = status
                doc["pages"] = n or 0
    except _Timeout:
        doc["status"] = "timeout"; pages = []
    except MemoryError:
        doc["status"] = "too_big"; pages = []
    except Exception:
        doc["status"] = "parse_fail"; pages = []
    finally:
        if timed:
            signal.setitimer(signal.ITIMER_REAL, 0)
    doc["text_pages"] = len(pages); doc["chars"] = sum(len(t) for _, t in pages)
    doc["secs"] = round(time.time() - t0, 1)
    return {"doc": doc, "pages": pages}


def _failed(task, status):
    return {"doc": {"id": task["id"], "rec": task["rec"], "att": task.get("att") or "", "status": status,
                    "pages": 0, "text_pages": 0, "chars": 0, "secs": 0.0,
                    "at": time.strftime("%Y-%m-%d", time.gmtime())}, "pages": []}


def run_batch(tasks, workers):
    """Extract one batch in parallel worker processes. A fresh pool per batch keeps pypdf's memory
    from accumulating over a five-hour run. If a document kills its worker (out of memory), the
    pool breaks: the survivors are kept, the rest are re-run one at a time, and the culprit is
    marked 'crash' so it never blocks the queue again."""
    done = {}
    try:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(extract_one, t): t for t in tasks}
            for f in as_completed(futs):
                try:
                    done[futs[f]["id"]] = f.result()
                except _Timeout:                          # the timer fired outside the guarded block
                    done[futs[f]["id"]] = _failed(futs[f], "timeout")
    except BrokenProcessPool:
        for t in tasks:
            if t["id"] in done:
                continue
            try:
                with ProcessPoolExecutor(max_workers=1) as ex:
                    done[t["id"]] = ex.submit(extract_one, t).result()
            except _Timeout:
                done[t["id"]] = _failed(t, "timeout")
            except BrokenProcessPool:
                done[t["id"]] = _failed(t, "crash")
    return [done[t["id"]] for t in tasks]


# ----------------------------------------------------------------------------------------------
# Planning
# ----------------------------------------------------------------------------------------------
def shard_of(lib_id, n):
    return int(hashlib.sha1(lib_id.encode("utf-8")).hexdigest()[:8], 16) % n


def needs_work(d, att, retry_failed):
    """Should a document be (re)extracted, given its line in the store's index (or None)?"""
    if d is None:
        return True
    if att and d.get("att") and d["att"] != att:          # the file was replaced
        return True
    if d["status"] in TERMINAL:
        return retry_failed and d["status"] in ("parse_fail", "timeout", "crash")
    return d.get("attempts", 1) < MAX_ATTEMPTS            # transient failure: try again, a few times


def load_index(store, work):
    listing = store.assets(refresh=True)
    docs_files, _ = tc.complete_parts(listing)
    tc.sync_down(store, docs_files, work)
    return tc.load_docs_index(work, docs_files), listing


def plan(args, store, work):
    types = None if args.types.strip().lower() == "all" else \
        ([t.strip() for t in args.types.split(",") if t.strip()] or CORE_TYPES)
    print(f">>> scope: {'ALL types' if types is None else str(len(types)) + ' types'}, "
          f"year >= {args.year_min}{'' if args.drop_undated else ' or undated'}", flush=True)
    docs = sweep_scope(types, args.year_min, not args.drop_undated)
    index, listing = load_index(store, work)
    queue = [d for d in docs if needs_work(index.get(d["id"]), d["att"], args.retry_failed)]
    queue.sort(key=lambda d: d["id"])
    # What is searchable must not depend on the scope of the LAST run: a narrow manual run would
    # otherwise shrink the metadata snapshot and every search with it. Documents already in the
    # store that this sweep did not return are re-read by record id and kept while they still
    # exist, still have a file and are not discarded.
    swept = {d["id"] for d in docs}
    earlier = [d["rec"] for i, d in index.items() if i not in swept and d.get("rec")]
    kept = refresh_rows(earlier) if earlier else []
    if earlier:
        print(f"    {len(earlier):,} documents in the store are outside this run's scope: "
              f"{len(kept):,} still valid and kept searchable, {len(earlier) - len(kept):,} gone or discarded.",
              flush=True)

    by_type = collections.defaultdict(lambda: [0, 0, 0, 0])      # docs, pages, searchable, queued
    in_q = {d["id"] for d in queue}
    for d in docs:
        r = by_type[d["type"] or "(untyped)"]
        r[0] += 1; r[1] += d["pages"]
        r[2] += (index.get(d["id"], {}).get("status") == "ok")
        r[3] += d["id"] in in_q
    print(f"\n    {'type':28}{'docs':>9}{'pages':>12}{'searchable':>12}{'queued':>9}")
    for t, r in sorted(by_type.items(), key=lambda kv: -kv[1][1]):
        print(f"    {t[:27]:28}{r[0]:>9,}{r[1]:>12,}{r[2]:>12,}{r[3]:>9,}")
    tot = [sum(r[i] for r in by_type.values()) for i in range(4)]
    print(f"    {'TOTAL':28}{tot[0]:>9,}{tot[1]:>12,}{tot[2]:>12,}{tot[3]:>9,}")
    status = collections.Counter(index[d["id"]]["status"] for d in docs if d["id"] in index)
    if status:
        print("    in the store, by outcome: " + ", ".join(f"{k} {v:,}" for k, v in status.most_common()))
    q_pages = sum(d["pages"] for d in queue) or len(queue) * 60
    companies = len({d["wba"] for d in docs if d["wba"]})
    shards = max(1, min(args.max_shards, math.ceil(q_pages / (PAGES_PER_SHARD_HOUR * SHARD_HOURS)))) if queue else 0
    hours = q_pages / PAGES_PER_SHARD_HOUR / max(shards, 1)
    line = (f"{len(docs):,} documents of {companies:,} companies in scope | queue: {len(queue):,} documents, "
            f"~{q_pages:,} pages | plan: {shards} shard(s), about {hours:.1f} h each at the planning rate")
    print(f"\n>>> {line}", flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:                                           # the plan, readable on the run page
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"**Plan**: {line}\n\n| type | docs | pages | searchable | queued |\n|---|--:|--:|--:|--:|\n")
            for t, r in sorted(by_type.items(), key=lambda kv: -kv[1][1]):
                f.write(f"| {t} | {r[0]:,} | {r[1]:,} | {r[2]:,} | {r[3]:,} |\n")
            f.write(f"| **Total** | {tot[0]:,} | {tot[1]:,} | {tot[2]:,} | {tot[3]:,} |\n\n")
            if status:
                f.write("In the store, by outcome: " + ", ".join(f"{k} {v:,}" for k, v in status.most_common()) + "\n\n")
    return docs + kept, queue, shards, listing


def publish_plan(store, work, docs, queue, listing):
    """Upload the metadata snapshot and the queue. Both are written under a NEW timestamped name
    and old ones are removed afterwards, so a reader never finds the file half-replaced."""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    meta_name = f"meta-{stamp}.jsonl.gz"; queue_name = f"queue-{stamp}.json.gz"
    keep = ("id", "rec", "wba", "co", "type", "year", "lang", "label", "url", "pages")
    tc.write_jsonl_gz(os.path.join(work, meta_name), ({k: d[k] for k in keep} for d in docs))
    store.upload(os.path.join(work, meta_name))
    with gzip.open(os.path.join(work, queue_name), "wt", encoding="utf-8") as f:
        json.dump([{"id": d["id"], "rec": d["rec"], "att": d["att"], "pages": d["pages"]} for d in queue], f)
    store.upload(os.path.join(work, queue_name))
    old_meta = sorted(n for n in listing if n.startswith("meta-") and n != meta_name)
    old_queue = sorted(n for n in listing if n.startswith("queue-") and n != queue_name)
    for n in old_meta[:-1] + old_queue[:-3]:              # keep one previous snapshot, three queues
        store.delete(n)
    return meta_name, queue_name


def github_output(**kv):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in kv.items():
                f.write(f"{k}={v}\n")


# ----------------------------------------------------------------------------------------------
# Extraction
# ----------------------------------------------------------------------------------------------
def doc_timeout(pages):
    """Seconds allowed for one document: DOC_TIMEOUT, stretched for long documents (a 900-page
    prospectus is slow, not stuck) and capped at half an hour."""
    return int(min(max(DOC_TIMEOUT, DOC_TIMEOUT * (pages or 0) / 150), 1800))


def tasks_for(batch, max_mb):
    """Attach a fresh download URL to each queued document of a batch."""
    cells = fresh_attachments([d["rec"] for d in batch])
    out = []
    for d in batch:
        a = pick_attachment(cells.get(d["rec"]), d.get("att"))
        out.append({"id": d["id"], "rec": d["rec"], "att": (a or {}).get("id") or d.get("att") or "",
                    "url": (a or {}).get("url"), "timeout": doc_timeout(d.get("pages")), "max_mb": max_mb})
    return out


def run_sample(queue, n, workers):
    batch = queue[:n]
    if not batch:
        return
    print(f"\n>>> DRY RUN sample: extracting {len(batch)} queued documents (nothing is stored)\n", flush=True)
    t0 = time.time()
    results = run_batch(tasks_for(batch, MAX_MB), workers)
    for r in results:
        d = r["doc"]
        first = (r["pages"][0][1][:110].replace("\n", " ") if r["pages"] else "")
        print(f"    [{d['status']:11}] {d['id']:24} pages={d['pages']:>4} with_text={d['text_pages']:>4} "
              f"chars={d['chars']:>8,} {d['secs']:>6.1f}s  {first}")
    pages = sum(r["doc"]["pages"] for r in results)
    dt = max(time.time() - t0, 0.1)
    print(f"\n    {pages:,} pages in {dt:.0f}s = {pages / dt:.1f} pages/s with {workers} workers "
          f"(the plan above assumes {PAGES_PER_SHARD_HOUR / 3600:.0f}/s per shard)")


def run_extract(args, store, work, queue, shard, shards):
    run = os.environ.get("GITHUB_RUN_ID") or f"local{os.getpid()}"
    run = f"r{run}a{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
    index, _ = load_index(store, work)                    # a re-run skips what a previous attempt stored
    mine = [d for d in queue if shard_of(d["id"], shards) == shard
            and needs_work(index.get(d["id"]), d.get("att"), args.retry_failed)]
    if args.limit:
        mine = mine[:args.limit]
    print(f">>> shard {shard + 1}/{shards}: {len(mine):,} documents to extract, {args.workers} workers, "
          f"budget {args.budget_min} min, store = {store.label}", flush=True)
    t0 = time.time(); seq = 0; writer = None
    counts = collections.Counter(); pages = 0; chars = 0; done = 0; parts = 0

    def flush():
        nonlocal writer, parts
        if writer is None or not writer.docs:
            return
        p_path, d_path = writer.close()
        store.upload(p_path)                              # text first ...
        store.upload(d_path)                              # ... then the done-marker
        os.remove(p_path); os.remove(d_path)
        parts += 1
        print(f"    part {writer.part} stored: {len(writer.docs)} documents, {writer.pages:,} pages", flush=True)
        writer = None

    stopped = False
    for i in range(0, len(mine), BATCH):
        elapsed = (time.time() - t0) / 60
        if elapsed + 12 > args.budget_min:                # leave room for a slow batch and the upload
            stopped = True
            break
        batch = mine[i:i + BATCH]
        for r in run_batch(tasks_for(batch, MAX_MB), args.workers):
            if writer is None:
                seq += 1
                writer = tc.PartWriter(work, tc.part_name(run, shard, seq))
            writer.add(r["doc"], r["pages"])
            counts[r["doc"]["status"]] += 1
            pages += r["doc"]["pages"]; chars += r["doc"]["chars"]; done += 1
        if writer and (len(writer.docs) >= FLUSH_DOCS or writer.chars >= FLUSH_CHARS
                       or time.time() - writer.opened >= FLUSH_SECONDS):
            flush()
        dt = max(time.time() - t0, 1)
        print(f"  {done:,}/{len(mine):,} documents  {pages:,} pages  {pages / dt:.1f} pages/s  {dict(counts)}", flush=True)
    flush()
    left = len(mine) - done
    dt = max(time.time() - t0, 1)
    print(f">>> DONE shard {shard + 1}/{shards}: {done:,} documents, {pages:,} pages, {chars / 1e6:.0f} MB of text, "
          f"{parts} part(s), {dt / 60:.0f} min, {pages / dt:.1f} pages/s. {dict(counts)}"
          + (f"  | {left:,} left for the next run (time budget reached)" if stopped and left else ""), flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"**Shard {shard + 1}/{shards}**: {done:,} documents, {pages:,} pages, {pages / dt:.1f} pages/s. "
                    + ", ".join(f"{k} {v:,}" for k, v in counts.most_common())
                    + (f". {left:,} left for the next run." if left else "") + "\n\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write to the store (default: dry run)")
    ap.add_argument("--plan", action="store_true", help="planning job: publish queue + metadata, print shards")
    ap.add_argument("--shard", default="", help="i/N: extract the i-th of N slices (1-based)")
    ap.add_argument("--queue", default="", help="name of a published queue file (with --shard)")
    ap.add_argument("--types", default="", help='comma-separated types, or "all" (default: report and policy families)')
    ap.add_argument("--year-min", type=int, default=2021, help="documents dated before this are left out")
    ap.add_argument("--drop-undated", action="store_true", help="leave out documents with no year")
    ap.add_argument("--limit", type=int, default=0, help="cap documents this run (0 = no cap)")
    ap.add_argument("--max-shards", type=int, default=8)
    ap.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 2))
    ap.add_argument("--budget-min", type=int, default=320, help="stop cleanly after this many minutes")
    ap.add_argument("--retry-failed", action="store_true", help="requeue parse_fail / timeout / crash")
    ap.add_argument("--sample", type=int, default=5, help="dry-run sample size")
    args = ap.parse_args()

    try:
        import cryptography  # noqa: F401
    except ImportError:
        sys.exit("pypdf needs the `cryptography` package to read AES-encrypted PDFs (most corporate "
                 "PDFs). It must be in requirements.txt; without it those files would all be "
                 "recorded as unreadable.")
    work = os.environ.get("TEXT_WORK_DIR") or tempfile.mkdtemp(prefix="fulltext_")
    os.makedirs(work, exist_ok=True)
    try:
        store = tc.open_store()
    except tc.StoreUnavailable as e:
        if args.commit:
            raise
        # A dry run must work before the store exists: it is how the plan gets sized.
        print(f">>> NOTE: {e}\n>>> The dry run continues as if the store were empty.", flush=True)
        store = tc.LocalStore(os.path.join(work, "empty_store"))
    print(f">>> text store: {store.label}", flush=True)

    if args.shard:                                        # one shard of a planned queue
        i, n = (int(x) for x in args.shard.split("/"))
        if not args.commit:
            sys.exit("--shard only makes sense with --commit.")
        if args.queue:
            store.assets(refresh=True)
            store.download(args.queue, os.path.join(work, args.queue))
            with gzip.open(os.path.join(work, args.queue), "rt", encoding="utf-8") as f:
                queue = json.load(f)
        else:
            _, queue, _, _ = plan(args, store, work)
        run_extract(args, store, work, queue, i - 1, n)
        return

    docs, queue, shards, listing = plan(args, store, work)
    if args.limit:
        queue = queue[:args.limit]
        shards = min(shards, 1) if queue else 0
        print(f">>> capped to --limit {args.limit} this run.", flush=True)

    if not args.commit:
        run_sample(queue, args.sample, args.workers)
        github_output(shards="[]", nshards=0, queue="")
        print("\n>>> dry run, nothing written. Re-run with --commit.", flush=True)
        return

    meta_name, queue_name = publish_plan(store, work, docs, queue, listing)
    print(f">>> published {meta_name} ({len(docs):,} documents) and {queue_name} ({len(queue):,} queued)", flush=True)
    if args.plan:
        github_output(shards=json.dumps(list(range(1, shards + 1))), nshards=shards, queue=queue_name)
        return
    if queue:                                             # no --plan: plan and extract in one process
        run_extract(args, store, work, queue, 0, 1)


if __name__ == "__main__":
    try:
        main()
    except tc.StoreError as e:
        sys.exit(f">>> STORE ERROR: {e}")
