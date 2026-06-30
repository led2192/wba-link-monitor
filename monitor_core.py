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
DOCISH   = re.compile(r"report|annual|sustainab|esg|/download|/publication|/disclosur", re.I)


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
    """Same-domain links that are PDFs or whose URL PATH looks report-ish.
    Returns {normalized_key: (fetchable_absolute_url, anchor_text)}.
    Anchor text is annotation only; it does not trigger a match."""
    rdom = reg_domain(base)
    out = {}
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absu = urljoin(base, href)
        if reg_domain(absu) != rdom:
            continue
        path = urlsplit(absu).path.lower()
        if path.endswith(".pdf") or DOCISH.search(path):
            n = normalize(absu)
            if n and n not in out:
                out[n] = (absu, a.get_text(" ", strip=True)[:80])
    return out


def page_language(html):
    """Best-effort primary language of a page. Tries the declared <html lang> attribute first
    (most reliable), then falls back to detecting from the visible text. Fully guarded: returns
    a short ISO code (e.g. "en", "de") or None, and never raises, so it can't break the monitor."""
    if not html:
        return None
    soup = None
    try:
        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find("html")
        if tag and tag.get("lang"):
            code = tag.get("lang").strip().lower().split("-")[0].split("_")[0]
            if code.isalpha() and 2 <= len(code) <= 3:
                return code
    except Exception:
        soup = None
    if not _HAVE_LD:
        return None
    try:
        if soup is None:
            soup = BeautifulSoup(html, "html.parser")
        for t in soup(["script", "style", "noscript"]):
            t.extract()
        text = soup.get_text(" ", strip=True)[:3000]
        if len(text) < 20:
            return None
        return _ld_detect(text)
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


def airtable_request(method, url, headers, payload=None, params=None, timeout=60, tries=5):
    """GET/POST/PATCH to Airtable, retrying transient failures (read timeouts, connection drops,
    429 rate limits, 5xx) with exponential backoff. A single slow response no longer kills a run.
    Prints Airtable's error body on a real 4xx (e.g. a bad field) before failing fast."""
    delay = 2
    for attempt in range(1, tries + 1):
        try:
            r = requests.request(method, url, headers=headers, json=payload, params=params, timeout=timeout)
        except requests.exceptions.RequestException:
            if attempt == tries:
                raise
            time.sleep(delay); delay = min(delay * 2, 30); continue
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == tries:
                print(f"Airtable {method} {r.status_code}: {r.text[:400]}")
                r.raise_for_status()
            time.sleep(delay); delay = min(delay * 2, 30); continue
        if r.status_code >= 400:
            print(f"Airtable {method} {r.status_code}: {r.text[:400]}")
        r.raise_for_status()
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
