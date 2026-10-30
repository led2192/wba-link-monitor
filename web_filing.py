#!/usr/bin/env python3
"""
web_filing.py: the text of an annual report filed as a web document, page by page.

Regulator filings (SEC 10-K, 20-F, 40-F on sec.gov; ESEF annual financial reports on
filings.xbrl.org) are HTML or XHTML files, not PDFs. regulator_filings.py adds them to
report_library when the company's own annual report for the year is missing, with no attachment.
Until 30 Oct 2026 nothing read them: build_fulltext.py only reads PDFs, so 655 filings (458 SEC,
197 ESEF) had a library row and no text, and the model reader never saw them.

These files are typeset for print and mark where each printed page ends (a CSS page break), so the
text can be split into the same pages a reader sees in the SEC or ESEF viewer. When a file has no
page breaks it is cut into blocks of about CHUNK characters, at paragraph boundaries.

The hidden inline XBRL header (ix:header, inside a display:none block) is dropped: it holds the
machine-readable contexts, not text a person reads.

Used by build_fulltext.py; pure functions, tested offline (tests/test_web_filing.py).
"""
import re
from urllib.parse import urlsplit

from text_core import clean_page_text

FILING_HOSTS = ("www.sec.gov", "sec.gov", "filings.xbrl.org")
CHUNK = 4000                 # characters per page when a filing has no page breaks
MIN_PAGE = 40                # characters: shorter pieces (a lone page number) join the next page
BREAK = re.compile(r"(?:page-break-(?:before|after)|break-(?:before|after))\s*:\s*(?:always|page)", re.I)
BLOCK_TAGS = {"p", "div", "tr", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "hr", "section"}
MARK = "\x00PAGEBREAK\x00"


def fetch_html(url, max_bytes, timeout=90):
    """(kind, text): 'ok' with the decoded HTML, 'too_big', or 'download_fail'. The SEC asks automated
    clients to name themselves in the User-Agent (the same contact as regulator_filings.py)."""
    import os, requests
    ua = {"User-Agent": f"World Benchmarking Alliance research {os.environ.get('SEC_CONTACT') or 'info@worldbenchmarkingalliance.org'}",
          "Accept-Encoding": "gzip, deflate"}
    try:
        with requests.get(url, headers=ua, timeout=timeout, stream=True) as r:
            if r.status_code != 200:
                return "download_fail", ""
            buf = bytearray()
            for chunk in r.iter_content(1 << 18):
                buf += chunk
                if len(buf) > max_bytes:
                    return "too_big", ""
            enc = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else "utf-8"
            return "ok", bytes(buf).decode(enc, errors="replace")
    except Exception:
        return "download_fail", ""


def is_filing_url(url):
    """True for a regulator filing the store can read as a web document."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if host in ("www.sec.gov", "sec.gov"):
        return "/Archives/edgar/data/" in parts.path and parts.path.lower().endswith((".htm", ".html", ".xhtml"))
    if host == "filings.xbrl.org":
        return parts.path.lower().endswith((".xhtml", ".html", ".htm"))
    return False


def _style_breaks(tag, where):
    st = tag.get("style") or ""
    for m in BREAK.finditer(st):
        if m.group(0).lower().startswith(("page-break-" + where, "break-" + where)):
            return True
    return False


def html_pages(html, chunk=CHUNK):
    """[(page_no, text)] from a filing's HTML. Page numbers start at 1."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "lxml") if _has_lxml() else BeautifulSoup(html or "", "html.parser")
    for t in soup.find_all(["script", "style", "noscript"]):
        t.decompose()
    for t in soup.find_all(re.compile(r"^ix:header$", re.I)):
        t.decompose()
    for t in soup.find_all(style=re.compile(r"display\s*:\s*none", re.I)):
        t.decompose()
    breaks = 0
    for t in soup.find_all(style=BREAK):
        if _style_breaks(t, "before"):
            t.insert_before(MARK); breaks += 1
        if _style_breaks(t, "after"):
            t.insert_after(MARK); breaks += 1
    for t in soup.find_all(BLOCK_TAGS):
        t.insert_after("\n")
    for t in soup.find_all(["td", "th"]):
        t.insert_after(" ")
    text = soup.get_text("")
    pieces = text.split(MARK) if breaks else _chunks(text, chunk)
    pages, carry = [], ""
    for p in pieces:
        t = clean_page_text(carry + "\n" + p if carry else p)
        if len(t) < MIN_PAGE:
            carry = t
            continue
        carry = ""
        pages.append(t)
    if carry and pages:
        pages[-1] = clean_page_text(pages[-1] + "\n" + carry)
    elif carry:
        pages.append(carry)
    return [(i + 1, t) for i, t in enumerate(pages) if t]


def _chunks(text, size):
    """Blocks of about `size` characters, cut at a line end (every paragraph and table row ends one)."""
    out, cur = [], []
    n = 0
    for para in re.split(r"(?<=\n)", text):
        cur.append(para); n += len(para)
        if n >= size:
            out.append("".join(cur)); cur, n = [], 0
    if cur:
        out.append("".join(cur))
    return out


def _has_lxml():
    try:
        import lxml  # noqa: F401
        return True
    except ImportError:
        return False
