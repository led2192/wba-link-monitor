#!/usr/bin/env python3
"""
table_text.py: a page's text with its tables kept as tables, for extract_eval --page-text.

The evidence database holds each page as plain text (pypdf), in which a table loses its rows and columns:
"Scope 1 18,981 17,402 2024 2023" no longer says which number is which year. The owner's research on search
engines (6 Nov 2026) points at structure-preserving parsers (Docling: 97.9% of table cells in place on
sustainability reports, against 75% for a plain extractor). This module re-reads only the pages the model
reader is about to see, from the PDF itself, and returns them as Markdown with each table as a Markdown table,
so a cell keeps its row label and column header.

Parsers:
  pymupdf   PyMuPDF's table finder through pymupdf4llm.to_markdown (fast, about a tenth of a second a page);
  docling   Docling's layout and table model (TableFormer), OCR off (about 1 to 6 seconds a page on CPU).

The PDF is the report_library attachment (fetched fresh, as build_fulltext.py does) or, for a file over
Airtable's 5 MB limit, its document_url. Regulator filings (web HTML) and anything that fails keep the
database text: the caller gets None for that page and counts it. Files are cached for the run in a temporary
folder, so the 8 pages of one document cost one download. Read-only.
"""
import os, tempfile, time
from urllib.parse import quote

CACHE = os.path.join(tempfile.gettempdir(), "table_text_pdfs")
MAX_BYTES = 150 * 1024 * 1024
_docling = None
STATS = {}
ERRORS = {}


def _count(k):
    STATS[k] = STATS.get(k, 0) + 1


def _record(lid):
    """The report_library fields of one document (file, document_url, file_status), or {}."""
    import build_fulltext as B
    request, url, headers = B._airtable()
    params = {"filterByFormula": f"{{{B.F_ID}}}='{lid}'", "maxRecords": 1,
              "fields[]": [B.F_ID, B.F_FILE, B.F_URL, B.F_FSTATUS]}
    recs = request("GET", url, headers, params=params).json().get("records", [])
    return recs[0].get("fields", {}) if recs else {}


def pdf_path(lid):
    """A local copy of the document's PDF, or None (no file, not a PDF, failed)."""
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, quote(lid, safe="") + ".pdf")
    if os.path.exists(p):
        return p if os.path.getsize(p) else None
    import build_fulltext as B
    try:
        f = _record(lid)
        att = B.pick_attachment(f.get(B.F_FILE))
        src = (att or {}).get("url") or (f.get(B.F_URL) if str(f.get(B.F_URL) or "").lower().split("?")[0].endswith(".pdf") else "")
        if not src:
            _count("no PDF"); open(p, "wb").close(); return None
        kind, data = B.fetch_pdf(src, MAX_BYTES)
    except Exception:
        kind, data = "download_fail", b""
    if kind != "ok":
        _count(f"download {kind}"); open(p, "wb").close(); return None
    with open(p, "wb") as out:
        out.write(data)
    return p


def _pymupdf(path, page):
    import pymupdf4llm
    return pymupdf4llm.to_markdown(path, pages=[page - 1], show_progress=False)


def _docling_md(path, page):
    global _docling
    if _docling is None:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
        opts = PdfPipelineOptions()
        opts.do_ocr = False
        opts.do_table_structure = True
        _docling = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
    res = _docling.convert(path, page_range=(page, page))
    return res.document.export_to_markdown()


PARSERS = {"pymupdf": _pymupdf, "docling": _docling_md}


def page_text(lid, page, parser):
    """The page as Markdown with its tables, or None (keep the database text)."""
    path = pdf_path(lid)
    if not path:
        return None
    t0 = time.time()
    try:
        md = PARSERS[parser](path, int(page))
    except Exception as e:
        _count("parse failed")
        if parser not in ERRORS:      # the first error of each parser, for the report (6 Nov 2026: Docling failed on
            ERRORS[parser] = f"{type(e).__name__}: {str(e)[:160]}"   # every page of its first run and nothing said why)
        print(f">>> table_text {parser}: {type(e).__name__}: {str(e)[:300]}", flush=True) if STATS["parse failed"] <= 3 else None
        return None
    STATS["seconds"] = STATS.get("seconds", 0.0) + time.time() - t0
    if not md or len(md.strip()) < 40:
        _count("empty page"); return None
    _count("pages parsed")
    STATS["tables"] = STATS.get("tables", 0) + md.count("\n|---") + md.count("\n| ---") + md.count("\n|:--")
    return md
