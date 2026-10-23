#!/usr/bin/env python3
"""
monitor_core.py — logic shared by the daily (requests) and weekly (Playwright) monitors.

Both monitors used to carry an identical copy of this code, which is why the same bugs lived
in both. The shared, fixed versions live here now; the monitors import them.

Three fixes vs the old duplicated logic:
  1. seen_links stores the FETCHABLE absolute URL (scheme + host incl. www + original case),
     not the normalize() dedup key. The key was lowercased and had scheme/www stripped, which
     is why links copied out of seen_links 404'd (3M's case-sensitive media ids, baicmotor.com
     needing www, missing https://). Change-detection still diffs on the normalized key, so this
     does not create false "new" links — see canon_seen() and detection_fields().
  2. post_detections() UPSERTS by detection_id instead of blind-creating, so re-detecting the
     same document updates the row in place rather than inserting a duplicate.
  3. detections sourced from a page whose type is in SKIP_DETECTION_TYPES (news) are not emitted.

normalize() is unchanged: it is the canonical dedup key and is meant to be lossy (lowercase,
no scheme, no www, trailing slash and tracking params dropped, query sorted). Keep it identical
to ids.normalize so link_id / detection_id stay stable.
"""
import re, time, hashlib
from urllib.parse import urljoin, urlsplit, parse_qsl, urlencode, quote

import requests
from bs4 import BeautifulSoup
import tldextract

# Optional language detection (used by page_language). Soft import so the monitor never
# breaks if the dependency is missing; the language field is simply skipped in that case.
try:
    from langdetect import detect as _ld_detect, DetectorFactory
    DetectorFactory.seed = 0
    _HAVE_LD = True
except Exception:
    _HAVE_LD = False

API = "https://api.airtable.com/v0"

# ---- Airtable field names (single source of truth; edit here if a field is renamed) ----
F_WBA="wba_id"; F_NAME="company_name"; F_URL="url"; F_FREQ="frequency"; F_MON="monitor"
F_TYPE="type"; F_STATUS="status"; F_HTTP="http_status"; F_FINAL="final_url"
F_CHECKED="last_checked"; F_HASH="content_hash"
F_SEEN="seen_links"; F_NEW="new_links"; F_CHANGE="last_change"; F_ALERT="alert_status"
F_BROWSER="needs_browser"
F_LANG="page_language"

# Page types whose detections we drop entirely (news pages produce press-release noise).
SKIP_DETECTION_TYPES = {"news"}

_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())
TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|_hs|ref$)", re.I)
DOCISH   = re.compile(r"report|annual|sustainab|esg|/download|/publication|/disclosur|/pdfs?/", re.I)  # /pdfs?/ catches extensionless CMS doc routes like /PDF/ModernSlaveryStatement (2sfg-style)
PDF_SEG  = re.compile(r"/pdfs?/", re.I)                   # hard doc signal for the raw-HTML sweep
ASSET_EXT = re.compile(r"\.(png|jpe?g|gif|svg|webp|ico|css|js|woff2?|ttf|mp4)([?#]|$)", re.I)
RAW_DOC_RX = re.compile(r"https?://[^\s\"'<>\\){}\[\]]+|(?<=[\"'])/[^\s\"'<>\\){}\[\]]+")
# Document-server pattern: modern CMSs (Sitecore/Next, etc.) serve every PDF from an opaque
# endpoint like /docs?documentId=... or ?editionId=..., where the URL PATH is just /docs and the
# only document signal is the query-string id. Path-only matching misses all of these.
DOC_ID_Q = re.compile(r"\b(document|edition|asset|media|file)[-_]?id=", re.I)

# Branded framework / standard-setter hosts. Cross-domain PDF policy, third iteration:
# v1 dropped ALL cross-domain raw PDFs (kept SBTi/GRI out, but silently lost every document of
# every headless-CMS company - the ACS/Sitecore case). v2 allowlisted known asset CDNs, which
# fixed ACS but is whack-a-mole: it missed Q4 (q4cdn.com, half of North-American IR), group
# parent domains, exchange-hosted filings (hkexnews) and every CDN not yet on the list.
# v3 (2026-07-17) inverts the burden, per the design principle "a PDF linked from the company's
# own monitored page is the company's document, wherever it is stored": cross-domain PDFs are
# ADMITTED by default and only these branded framework publishers are excluded, because their
# PDFs are standards/initiative material, never a specific company's disclosure. The remaining
# risk (an unknown shared third-party PDF linked by many companies) is handled downstream by the
# shared-document threshold in harvest_reports (SHARED_DOC_MIN), mirroring unmonitor_shared's
# page-level logic at document level. Suffix-matched on the hostname.
# Extensionless document endpoints (2026-07-27, Abercrombie/Q4 case): several corporate
# platforms serve every document from an opaque path with no .pdf extension and no
# documentId query. Q4 Inc ("/static-files/{uuid}") powers thousands of listed companies'
# IR sites; Kentico uses "/getattachment/". These paths ARE the document: they count as
# doc-shaped in both extraction passes and the harvester accepts them. Add a segment here
# when a new platform shows the Abercrombie signature: page renders fine, anchors visible,
# zero doc links extracted.
DOC_ENDPOINTS = ("/static-files/", "/getattachment/")
# Extensionless "/files/<slug>" (2026-10-19, Aldi Sud case): the sustainability site serves every
# report as /files/2022-climate-protection-progress-report with no extension. The monitor kept
# them (report-ish path) but the harvester rejected them as not PDF-shaped, so 13 reports sat in
# seen_links for months. A last segment WITHOUT a dot under /files/ is treated as a document;
# /files/x.jpg or /files/x.docx keep their own extension rules.
_FILES_SLUG = re.compile(r"/files/[^/.]+/?$")
# Liferay document library (2026-10-20, Asia Pulp & Paper case): files are served as
# /documents/<group>/<folder>/<File Name>.pdf/<uuid>, so the path ends in the uuid, not ".pdf".
# Only the ones whose name happened to contain a report word were caught; APP's 2021-2025
# sustainability reports ("SR_PT_APP_..._2025_ENGLISH.pdf/adff5ea3-...") were never seen.
# A ".pdf/" segment followed by one last segment is the document itself.
_PDF_THEN_ID = re.compile(r"\.pdf/[^/]+/?$")


def is_doc_endpoint(path):
    p = (path or "").lower()
    return (any(seg in p for seg in DOC_ENDPOINTS) or bool(_FILES_SLUG.search(p))
            or bool(_PDF_THEN_ID.search(p)))


FRAMEWORK_SUFFIXES = (
    "sciencebasedtargets.org", "globalreporting.org", "weps.org", "unglobalcompact.org",
    "cdp.net", "sasb.org", "ifrs.org", "integratedreporting.org", "wbcsd.org",
    "ghgprotocol.org", "iso.org", "weforum.org", "oecd.org", "ilo.org", "un.org",
    "europa.eu", "tcfdhub.org", "fsb.org", "msci.com", "sustainalytics.com",
    "ecovadis.com", "ellenmacarthurfoundation.org", "fairlabor.org", "bcorporation.net",
)


# Digital-asset-management platforms (2026-10-20, McCormick case): companies host their reports on
# a DAM under the vendor's domain, e.g. mccormick.widen.net/s/<id>/2025_plp_report_final. Those
# links are cross-domain and have no .pdf, so they were dropped before harvest ever saw them. A
# report-ish link to one of these hosts is kept; doc_probe then opens it and only real PDFs are
# harvested.
# Status codes that mean "the site refuses robots", not "the page is gone" (ADB, ASML, Air Products,
# ~2,000 monitored pages in Oct 2026, all labelled dead while working fine in a normal browser).
# Those pages get status 'blocked'. We do not try to get around the block.
BLOCKED_CODES = {401, 403, 429}

DAM_SUFFIXES = ("widen.net", "widencdn.net", "bynder.com", "brandfolder.com", "brandfolder.io",
                "frontify.com", "canto.com", "ctfassets.net")


def is_dam_host(host):
    host = (host or "").lower()
    return any(host == s or host.endswith("." + s) for s in DAM_SUFFIXES)


def is_framework_host(host):
    host = (host or "").lower()
    return any(host == s or host.endswith("." + s) for s in FRAMEWORK_SUFFIXES)


def reg_domain(u):
    e = _EXTRACT(u)
    return f"{e.domain}.{e.suffix}".lower() if e.suffix else (e.domain or "").lower()


def normalize(url):
    """Canonical dedup key. Lossy by design; must match ids.normalize()."""
    try:
        s = urlsplit(url)
    except Exception:
        return None
    if s.scheme not in ("http", "https"):
        return None
    host = (s.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+$", "", s.path)
    q = sorted((k, v) for k, v in parse_qsl(s.query) if not TRACKING.match(k))
    return (host + path + ("?" + urlencode(q) if q else "")).lower()


def canon_seen(x):
    """Reduce a stored seen_links entry OR a fresh URL to the canonical key.

    Old rows stored normalize() keys (no scheme); new rows store the fetchable absolute URL
    (with scheme). Both must reduce to the same key so change-detection keeps matching across
    the format switch and does not flag everything as new on the first run after deploy.
    """
    x = (x or "").strip()
    if not x:
        return None
    if x.startswith(("http://", "https://")):
        return normalize(x)        # fresh fetchable URL -> key
    return x                       # already a normalize() key from a previous run


def doc_links(html, base):
    """Links that are PDFs, report-ish paths, or CMS document-server endpoints.
    Returns {normalized_key: (fetchable_absolute_url, anchor_text)}.
    Anchor text is annotation only; it does not trigger a match.

    Same-domain links are kept if they look like a document (.pdf, a DOCISH path, or a
    documentId/editionId query). Cross-domain links are kept ONLY when they carry a
    documentId/editionId query (the company's own CMS content on a sibling domain), OR when they are
    a .pdf: a PDF linked from the company's own monitored page is treated as the company's document
    wherever it is stored (CDNs, IR providers like Q4 or MZiq, group domains, exchange hosts).
    Extensionless platform endpoints (DOC_ENDPOINTS: Q4 /static-files/, Kentico /getattachment/)
    are documents too, same-domain and cross-domain alike.
    Only branded framework/standard-setter/ratings publishers (FRAMEWORK_SUFFIXES) stay excluded,
    and mass-shared documents are additionally filtered at harvest by the SHARED_DOC_MIN
    threshold. Page discovery (DOCISH paths) remains same-domain only: third-party PAGES never
    become monitoring candidates."""
    rdom = reg_domain(base)
    out = {}
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        try:
            absu = urljoin(base, href)
            s = urlsplit(absu)
            path = (s.path or "").lower()
            is_doc_q = bool(DOC_ID_Q.search(s.query or ""))      # /docs?documentId=... CMS endpoint
            is_document = path.endswith(".pdf") or DOCISH.search(path) or is_doc_q or is_doc_endpoint(path)
            if not is_document:
                continue
            xdom_pdf = (path.endswith(".pdf") or is_doc_endpoint(path)) and not is_framework_host(s.hostname)
            if reg_domain(absu) != rdom and not (is_doc_q or xdom_pdf or is_dam_host(s.hostname)):  # cross-domain: CMS docs, non-framework PDFs, DAM-hosted docs
                continue
            n = normalize(absu)
        except ValueError:
            continue          # malformed href (an unclosed '[' parses as a broken IPv6 host)
        if n and n not in out:
            out[n] = (absu, a.get_text(" ", strip=True)[:80])

    # Raw-HTML sweep: JS-rendered sites often carry their document URLs inside inline JSON/config
    # that a client script turns into links later (2sfg-style), so the anchor pass above never
    # sees them. Scan the raw HTML for HARD document URLs only — .pdf files, /pdf/ CMS routes, or
    # documentId queries. DOCISH words are deliberately NOT reused here: free-text matches inside
    # scripts would sweep in navigation junk. Cross-domain policy identical to the anchor pass.
    raw = html[:500000].replace("\\/", "/")               # unescape JSON-escaped slashes
    for m in RAW_DOC_RX.finditer(raw):
        try:
            absu = urljoin(base, m.group(0))
            s = urlsplit(absu)
            path = (s.path or "").lower()
            if ASSET_EXT.search(path):
                continue
            is_doc_q = bool(DOC_ID_Q.search(s.query or ""))
            if not (path.endswith(".pdf") or PDF_SEG.search(path) or is_doc_q or is_doc_endpoint(path)):
                continue
            if reg_domain(absu) != rdom and not (is_doc_q or ((path.endswith(".pdf") or is_doc_endpoint(path)) and not is_framework_host(s.hostname))):
                continue
            n = normalize(absu)
        except Exception:
            # Sweep candidates come from raw JS and can be URL-SHAPED junk (regex sources like
            # "https://[a-z0-9]+...", template fragments). urlsplit raises ValueError on such
            # strings ("Invalid IPv6 URL", the 2026-07-05 run killer). A junk candidate must
            # never kill a run: skip it and move on.
            continue
        if n and n not in out:
            out[n] = (absu, "")
    return _drop_truncated(out)


def _drop_truncated(out):
    """The raw-HTML sweep stops at whitespace, so a file name with spaces ("Boparan Animal welfare
    policy V8 2026.pdf", 2 Sisters Food Group, 2026-10-20) also yields a chopped copy
    (.../Agriculture/Boparan) next to the full link found by the anchor pass. A link that is the
    full link cut at a space is not an address: drop it."""
    full = [v[0] for v in out.values()]
    keep = {}
    for k, v in out.items():
        u = v[0]
        if any(o != u and (o.startswith(u + " ") or o.startswith(u + "%20")) for o in full):
            continue
        keep[k] = v
    return keep


LANG_NAMES = {
    "af":"Afrikaans","ar":"Arabic","bg":"Bulgarian","bn":"Bengali","ca":"Catalan",
    "cs":"Czech","cy":"Welsh","da":"Danish","de":"German","el":"Greek","en":"English",
    "es":"Spanish","et":"Estonian","fa":"Persian","fi":"Finnish","fr":"French",
    "gu":"Gujarati","he":"Hebrew","hi":"Hindi","hr":"Croatian","hu":"Hungarian",
    "id":"Indonesian","it":"Italian","ja":"Japanese","kn":"Kannada","ko":"Korean",
    "lt":"Lithuanian","lv":"Latvian","mk":"Macedonian","ml":"Malayalam","mr":"Marathi",
    "ne":"Nepali","nl":"Dutch","no":"Norwegian","pa":"Punjabi","pl":"Polish",
    "pt":"Portuguese","ro":"Romanian","ru":"Russian","sk":"Slovak","sl":"Slovenian",
    "so":"Somali","sq":"Albanian","sv":"Swedish","sw":"Swahili","ta":"Tamil",
    "te":"Telugu","th":"Thai","tl":"Tagalog","tr":"Turkish","uk":"Ukrainian",
    "ur":"Urdu","vi":"Vietnamese","zh-cn":"Chinese (Simplified)","zh-tw":"Chinese (Traditional)",
}


def lang_name(code):
    """Map a detector/HTML language code to a readable name; fall back to the raw code if unknown."""
    if not code:
        return None
    return LANG_NAMES.get(code) or LANG_NAMES.get(code.split("-")[0]) or code


def page_language(html):
    """Best-effort primary language of a page. Tries the declared <html lang> attribute first
    (most reliable), then falls back to detecting from the visible text. Returns a readable
    language name (e.g. "English", "German") or None, and never raises, so it can't break the monitor."""
    if not html:
        return None
    soup = None
    try:
        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find("html")
        if tag and tag.get("lang"):
            code = tag.get("lang").strip().lower().split("-")[0].split("_")[0]
            if code.isalpha() and 2 <= len(code) <= 3:
                return lang_name(code)
    except Exception:
        soup = None
    if not _HAVE_LD:
        return None
    try:
        if soup is None:
            soup = BeautifulSoup(html, "html.parser")
        for t in soup(["script", "style", "noscript"]):
            t.extract()
        text = soup.get_text(" ", strip=True)
        if sum(ch.isalpha() for ch in text) < 30:
            return None
        return lang_name(_ld_detect(text[:10000]))
    except Exception:
        return None


def detection_fields(f, upd, current, had_baseline, today):
    """Diff the page's report links vs what we saw last time, write seen_links/new_links,
    gate the alert, and build the detection rows. `today` is a datetime.date.
    Returns (changed, high_signal, docs)."""
    cur = set(current)                                   # set of normalized keys
    upd[F_HASH] = hashlib.md5("\n".join(sorted(cur)).encode()).hexdigest()

    # Diff on the canonical key, canonicalizing the stored side so it works whether seen_links
    # holds old keys or new fetchable URLs (no false "new" on the format switch).
    seen = set(filter(None, (canon_seen(x) for x in (f.get(F_SEEN) or "").split("\n"))))
    new = sorted(cur - seen)

    # Store the FETCHABLE absolute URL, not the dedup key, so links are clickable/downloadable.
    upd[F_SEEN] = "\n".join(current[n][0] for n in sorted(cur))[:90000]

    if not (had_baseline and new):                       # first visit = baseline, no false alert
        return False, False, []

    # News pages: keep liveness (seen/hash above) but emit no detections and no alert.
    if (f.get(F_TYPE) or "").strip().lower() in SKIP_DETECTION_TYPES:
        return False, False, []

    entries = []; docs = []
    for n in new:
        disp, t = current.get(n, ("", ""))
        t = (t or "").strip()
        entries.append(n + (f"  [{t[:70]}]" if t else ""))
        _doc = disp or ("https://" + n)                  # disp is the fetchable absolute URL
        _page = f.get(F_URL, ""); _wba = f.get(F_WBA, "")
        docs.append({"detected": today.isoformat(), "wba_id": _wba,
                     "company_name": f.get(F_NAME, ""), "document_url": _doc,
                     "title": t, "found_on": _page, "page_type": f.get(F_TYPE, ""),
                     "is_pdf": n.endswith(".pdf"), "status": "new",
                     "source_link_id": link_id(_wba, _page),
                     "detection_id": detection_id(_wba, _page, _doc)})
    line = f"{today.isoformat()}: " + " ; ".join(entries)
    old = (f.get(F_NEW) or "").strip()
    upd[F_NEW] = (line + ("\n" + old if old else ""))[:90000]      # newest first, history kept
    upd[F_CHANGE] = today.isoformat()
    high = any(n.endswith(".pdf") for n in new) or (f.get(F_TYPE) in ("reports_hub", "sustainability_page"))
    if high:
        upd[F_ALERT] = "new"
    return True, high, docs


def is_formula_error(v):
    """Airtable returns a formula that fails on a row as {"error": "#ERROR!"} or {"specialValue": "NaN"}
    instead of a string or number. (AI fields come back as {"state": ..., "value": ...}: not errors.)"""
    return isinstance(v, dict) and ("error" in v or "specialValue" in v) and "state" not in v


def clean_records(data):
    """Formula errors in read records become None, so a script reading a text field never meets a dict
    (company_coverage crashed on doc_label in an error state, 23 Oct 2026)."""
    if isinstance(data, dict):
        for rec in data.get("records", []) or []:
            f = rec.get("fields") if isinstance(rec, dict) else None
            if isinstance(f, dict):
                for k in [k for k, v in f.items() if is_formula_error(v)]:
                    f[k] = None
    return data


def airtable_request(method, url, headers, payload=None, params=None, timeout=60, tries=5):
    """GET/POST/PATCH to Airtable, retrying transient failures (read timeouts, connection drops,
    429 rate limits, 5xx) with exponential backoff. On timeout retries the per-request timeout
    ESCALATES (60 -> 96 -> 153 -> 240s) instead of reusing the same ceiling: during the
    2026-07-12 Airtable slow window, five identical 60s attempts all failed inside the same
    ~5-minute incident. Escalating headroom plus a longer backoff ceiling lets the ladder ride
    out a degraded-API window. Prints Airtable's error body on a real 4xx before failing fast."""
    delay = 2
    for attempt in range(1, tries + 1):
        try:
            r = requests.request(method, url, headers=headers, json=payload, params=params, timeout=timeout)
        except requests.exceptions.Timeout:
            if attempt == tries:
                raise
            nxt = min(int(timeout * 1.6), 240)
            print(f"Airtable {method} timed out after {timeout}s (attempt {attempt}/{tries}); "
                  f"retrying in {delay}s with a {nxt}s timeout", flush=True)
            timeout = nxt
            time.sleep(delay); delay = min(delay * 2, 90); continue
        except requests.exceptions.RequestException as e:
            if attempt == tries:
                raise
            print(f"Airtable {method} {type(e).__name__} (attempt {attempt}/{tries}); "
                  f"retrying in {delay}s", flush=True)
            time.sleep(delay); delay = min(delay * 2, 90); continue
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == tries:
                print(f"Airtable {method} {r.status_code}: {r.text[:400]}")
                r.raise_for_status()
            time.sleep(delay); delay = min(delay * 2, 90); continue
        if r.status_code >= 400:
            print(f"Airtable {method} {r.status_code}: {r.text[:400]}")
        r.raise_for_status()
        if method.upper() == "GET":
            try:
                data = clean_records(r.json())
                r.json = lambda *a, **k: data
            except ValueError:
                pass
        return r
    return r


def post_detections(rows, base, token, table):
    """One row per detected document. UPSERTS on detection_id so re-detections update in place
    instead of duplicating. Non-fatal if the table is missing.

    NOTE: upsert requires detection_id to be unique in the table. Run clean_detections.py first
    to remove the existing duplicates; otherwise Airtable rejects a batch that matches >1 row.
    """
    if not rows:
        return
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        for i in range(0, len(rows), 10):
            payload = {"performUpsert": {"fieldsToMergeOn": ["detection_id"]},
                       "records": [{"fields": x} for x in rows[i:i + 10]],
                       "typecast": True}
            airtable_request("PATCH", url, headers, payload); time.sleep(0.2)
    except Exception as e:
        print(f"WARNING: could not write detections to '{table}' ({e}).")


# imported at the bottom to avoid a circular import surprise if ids ever imports from here
from ids import link_id, detection_id  # noqa: E402
