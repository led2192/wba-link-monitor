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

def _h(s, n):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:n]

def link_id(wba_id, url):
    return f"{(str(wba_id).strip() or 'NA')}-{_h(normalize(url), 8)}"

def detection_id(wba_id, found_on, document_url):
    return f"{(str(wba_id).strip() or 'NA')}-{_h(normalize(found_on) + '|' + normalize(document_url), 10)}"
