#!/usr/bin/env python3
"""
ids.py — shared, deterministic IDs so monitored_links and detections join EXACTLY.

  link_id(wba_id, url)                -> "{wba_id}-{8hex}"   identifies a watched page
  detection_id(wba_id, found_on, doc) -> "{wba_id}-{10hex}"  identifies one detected document

A detection's source_link_id is computed as link_id(wba_id, found_on). Because found_on is the
page's own URL and the page's link_id is link_id(wba_id, page_url), the two are identical by
construction — so a detection joins to its source row with no URL string-matching. IDs are
stable across re-imports (derived from the normalized URL, not from row order or Airtable's
internal record id). The wba_id prefix makes the id readable (you can see the company at a glance).
"""
import re, hashlib
from urllib.parse import urlsplit, parse_qsl, urlencode

TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|_hs|ref$)", re.I)

def normalize(url):
    try:
        s = urlsplit(url or "")
    except Exception:
        return (url or "").strip().lower()
    if s.scheme not in ("http", "https"):
        return (url or "").strip().lower()
    host = (s.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+$", "", s.path)
    q = sorted((k, v) for k, v in parse_qsl(s.query) if not TRACKING.match(k))
    return (host + path + ("?" + urlencode(q) if q else "")).lower()

# ---------------------------------------------------------------------------------------------
# doc_key: the identity of a DOCUMENT, as opposed to the identity of a URL.
#
# normalize() is the URL identity and stays untouched (link_id and library_id depend on it).
# doc_key() goes further and strips the parts of a document URL that change on every visit
# without changing the file, so the harvester can tell "same document, new URL" from "new
# document". Measured on report_library in October 2026, these per-visit URLs were the main
# source of ~53k duplicate rows (15.6% of the table). Each rule is tied to a pattern in the data:
#   - Q4 platform rotating segment: /_assets/_<32 hex>/... and cloudfront /_<32 hex>/...
#     (Coca-Cola, Walmart, Apollo, Dollar Tree, CBRE, Huntsman...)
#   - signed / expiring query parameters (x-expires, x-signature, AWS, CloudFront, Azure SAS)
#   - cache-busting numeric parameters (?v=1786794420935, ?m=20260413114243)
#   - ?token=<base64 file path>|<signature> (Federated Hermes): keep the path, drop the rest
#   - TYPO3 securedl: /securedl/sdl-<JWT> where the JWT payload names the file (Symrise)
#   - numbered CDN shards: cdn0. vs cdn. , sf26-sign. vs sf9-sign.
#   - HTML-escaped ampersands (&amp;) left in scraped hrefs
# ---------------------------------------------------------------------------------------------
_ROTATING_SEGMENT = re.compile(r"^_[0-9a-f]{32}$")
_VOLATILE_PARAMS = {
    "x-expires", "x-signature", "lk3s", "expires", "signature", "sig", "key-pair-id", "policy",
    "se", "sp", "sv", "sr", "st", "spr", "skoid", "sktid", "skt", "ske", "sks", "skv",
    "force", "download", "dl",
}
_CACHE_PARAMS = {"v", "ver", "version", "m", "t", "ts", "timestamp", "cb", "_", "rev", "time",
                 "modified", "updated"}
_NUMERIC = re.compile(r"^\d{6,}$")
_SHARD = re.compile(r"^([a-z]+)\d+([.-])")


def _securedl_file(path):
    """TYPO3 securedl: the JWT payload carries the real file path. Returns it, or None."""
    import base64, json
    m = re.search(r"/securedl/sdl-([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)", path)
    if not m:
        return None
    try:
        seg = m.group(2) + "=" * (-len(m.group(2)) % 4)
        return json.loads(base64.urlsafe_b64decode(seg)).get("file")
    except Exception:
        return None


def doc_key(url):
    """Canonical key of the document behind a URL. Two URLs with the same doc_key serve the same
    file. Used for de-duplication only, never as a fetchable URL."""
    raw = (url or "").strip().replace("&amp;", "&")
    try:
        s = urlsplit(raw)
    except Exception:
        return normalize(raw)
    if s.scheme not in ("http", "https"):
        return normalize(raw)
    host = (s.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    host = _SHARD.sub(r"\1\2", host)
    sdl = _securedl_file(s.path)
    if sdl:
        return (host + "/securedl/" + sdl).lower()
    segs = [p for p in s.path.split("/") if p and not _ROTATING_SEGMENT.match(p.lower())]
    path = "/" + "/".join(segs)
    q = []
    for k, v in parse_qsl(s.query, keep_blank_values=True):
        kl = k.lower()
        if TRACKING.match(k) or kl in _VOLATILE_PARAMS or kl.startswith("x-amz-"):
            continue
        if kl in _CACHE_PARAMS and (not v or _NUMERIC.match(v)):
            continue
        if kl == "token" and "|" in v:
            v = v.split("|", 1)[0]
        q.append((kl, v))
    q.sort()
    return (host + path.rstrip("/") + ("?" + urlencode(q) if q else "")).lower()


def doc_id(wba_id, url):
    """Company-scoped document identity: '{wba_id}|{doc_key}'. The harvester refuses to create a
    library row whose doc_id is already in the table (discarded rows included)."""
    return f"{(str(wba_id).strip() or 'NA')}|{doc_key(url)}"


def _h(s, n):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:n]

def link_id(wba_id, url):
    return f"{(str(wba_id).strip() or 'NA')}-{_h(normalize(url), 8)}"

def detection_id(wba_id, found_on, document_url):
    return f"{(str(wba_id).strip() or 'NA')}-{_h(normalize(found_on) + '|' + normalize(document_url), 10)}"
