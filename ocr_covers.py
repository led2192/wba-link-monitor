#!/usr/bin/env python3
"""
ocr_covers.py: read the first pages of scanned PDFs with text recognition (OCR).

Build cover text marks a PDF cover_status = no_text when it parses but holds no text: a scan, pages
that are images. Those rows get no AI type, no title, no year from the text, and count as
DOC_UNREADABLE in Pipeline QA. This job takes them (not discarded, with a file, or too big to attach
and read from document_url), up to --limit a run, renders the first --pages pages as images
(pdftoppm, 150 dpi) and reads them with Tesseract (English model: it also reads other Latin-script
languages, without some accents; six models at once timed out on the runner). When at least 200 letters come out, it writes cover_text, doc_language, cover_status = ok and
ticks cover_ocr, so the AI type, title, year and sustainability class follow like for any other file.
Otherwise it writes cover_status = no_text_ocr, so the row is not tried again every month.

Needs the poppler-utils and tesseract-ocr packages (installed by the workflow). Dry run by default
(prints and writes ocr_covers.md); --commit writes.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE; QA: AIRTABLE_QA_TOKEN (optional). Args: --limit 600 --pages 6 --workers 4 --minutes 0 --chunk 40
"""
import argparse, collections, os, re, subprocess, tempfile, time
from concurrent.futures import ThreadPoolExecutor

import qa
import build_cover_text as BCT
from monitor_core import airtable_request

LANGS = os.environ.get("OCR_LANGS", "eng")   # 6 languages at once timed out (300 s a page) on the runner
MIN_LETTERS = 200


def letters(text):
    return sum(ch.isalpha() for ch in text or "")


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()[:BCT.COVER_CHARS]


def outcome(text):
    """(cover_status, text) for the OCR result of one document."""
    t = clean(text)
    return ("ok", t) if letters(t) >= MIN_LETTERS else ("no_text_ocr", "")


def ocr_pdf(data, pages, dpi=150):
    """Text of the first `pages` pages of a PDF given as bytes, by Tesseract."""
    with tempfile.TemporaryDirectory() as d:
        pdf = os.path.join(d, "doc.pdf")
        with open(pdf, "wb") as fh:
            fh.write(data)
        # pdftoppm exits non-zero on slightly damaged PDFs while still rendering the pages: judge by the images
        r = subprocess.run(["pdftoppm", "-f", "1", "-l", str(pages), "-r", str(dpi), "-png", pdf, os.path.join(d, "p")],
                           capture_output=True, timeout=300)
        if not any(f.endswith(".png") for f in os.listdir(d)):
            raise RuntimeError("pdftoppm rendered no page: " + r.stderr.decode("utf-8", "ignore")[:120])
        out = []
        for img in sorted(f for f in os.listdir(d) if f.endswith(".png")):
            # one thread per tesseract: with 4 workers each starting 4 threads, 22 of 25 pages timed out (24 Oct 2026)
            r = subprocess.run(["tesseract", os.path.join(d, img), "stdout", "-l", LANGS, "--psm", "3"],
                               capture_output=True, timeout=120, env={**os.environ, "OMP_THREAD_LIMIT": "1"})
            out.append(r.stdout.decode("utf-8", "ignore"))
        return "\n".join(out)


def fetch_rows(limit):
    formula = ("AND({cover_status}='no_text', NOT({discard}), "
               "OR({file}, AND({file_status}='too_big', {document_url}!='')))")
    params = [("pageSize", "100"), ("filterByFormula", formula), ("fields[]", "file"), ("fields[]", "document_url")]
    out, offset = [], None
    while True:
        p = params + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", BCT.API, BCT.H, params=p).json()
        out += j.get("records", [])
        offset = j.get("offset")
        if not offset or (limit and len(out) >= limit):
            break
        time.sleep(0.15)
    return out[:limit] if limit else out


def fresh(recs):
    """The same rows re-read just before use: Airtable attachment links expire after about two hours,
    and a 600-document run takes longer (23 Oct 2026: 492 of 600 downloads failed)."""
    if not recs:
        return []
    formula = "OR(" + ",".join(f"RECORD_ID()='{r['id']}'" for r in recs) + ")"
    params = [("pageSize", "100"), ("filterByFormula", formula), ("fields[]", "file"), ("fields[]", "document_url")]
    got = {r["id"]: r for r in airtable_request("GET", BCT.API, BCT.H, params=params).json().get("records", [])}
    return [got[r["id"]] for r in recs if r["id"] in got]


def source(rec):
    return "file" if rec.get("fields", {}).get("file") else "link"


def one(rec, pages, deadline=None):
    if deadline and time.time() > deadline:
        return rec["id"], "not_tried", "", None
    url = BCT.attachment_url(rec)
    if not url:
        return rec["id"], "skip", "", None
    kind, data = BCT.download_pdf(url)
    if kind != "ok":
        return rec["id"], f"download_{kind}_{source(rec)}", "", None
    try:
        status, text = outcome(ocr_pdf(data, pages))
    except Exception as e:
        return rec["id"], "ocr_error", f"{type(e).__name__}: {str(e)[:120]}", None
    return rec["id"], status, text, BCT.detect_lang(text) if status == "ok" else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=600)
    ap.add_argument("--pages", type=int, default=6)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--minutes", type=float, default=0, help="stop starting new documents after this long (0 = no limit)")
    ap.add_argument("--chunk", type=int, default=40, help="rows re-read together just before they are processed")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("ocr_covers.py")
    rows = fetch_rows(a.limit)
    print(f">>> {len(rows):,} scanned covers to read", flush=True)
    results, counts, langs = [], collections.Counter(), collections.Counter()
    deadline = time.time() + a.minutes * 60 if a.minutes else None
    i = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for c in range(0, len(rows), a.chunk):
            if deadline and time.time() > deadline:
                counts["not_tried"] += len(rows) - c
                break
            for res in ex.map(lambda r: one(r, a.pages, deadline), fresh(rows[c:c + a.chunk])):
                i += 1
                results.append(res)
                counts[res[1]] += 1
                if res[3]:
                    langs[res[3]] += 1
                if i % 25 == 0:
                    print(f"  {i}/{len(rows)} {dict(counts)}", flush=True)
    md = ["### Scanned covers read by OCR" + ("" if a.commit else " (dry run)"), "",
          f"Scanned documents tried: {len(results):,}. Read: {counts['ok']:,}; still no text: {counts['no_text_ocr']:,}; "
          f"download failed: {sum(v for k, v in counts.items() if k.startswith('download_')):,} "
          f"(attachments {sum(v for k, v in counts.items() if k.startswith('download_') and k.endswith('_file')):,}, "
          f"links {sum(v for k, v in counts.items() if k.startswith('download_') and k.endswith('_link')):,}); "
          f"OCR error: {counts['ocr_error']:,}; not tried (time budget): {counts['not_tried']:,}.", "",
          "Languages of the covers read: " + (", ".join(f"{k} {v}" for k, v in langs.most_common(8)) or "none") + ".", "",
          "Samples (first 160 characters):", ""]
    errs = collections.Counter(t.split(":")[0] for _, s_, t, _ in results if s_ == "ocr_error")
    md += [f"- {t[:160]}" for _, s, t, _ in results if s == "ok"][:8]
    if errs:
        md += ["", "OCR errors: " + ", ".join(f"{k} {v}" for k, v in errs.most_common()),
               "First error: " + next(t for _, s_, t, _ in results if s_ == "ocr_error")]
    open("ocr_covers.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    if a.commit:
        recs = []
        for rid, status, text, lang in results:
            if status == "ok":
                f = {"cover_text": text, "cover_status": "ok", "cover_ocr": True}
                if lang:
                    f["doc_language"] = lang
                recs.append({"id": rid, "fields": f})
            elif status == "no_text_ocr":
                recs.append({"id": rid, "fields": {"cover_status": "no_text_ocr"}})
        for i in range(0, len(recs), 10):
            airtable_request("PATCH", BCT.API, BCT.H, {"records": recs[i:i + 10], "typecast": True})
            time.sleep(0.25)
    run.finish("ok", rows_in=len(rows), rows_out=counts["ok"], counters=dict(counts))
    run.write_step_summary("ok", dict(counts), title="OCR covers")


if __name__ == "__main__":
    main()
