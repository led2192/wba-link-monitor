#!/usr/bin/env python3
"""
classify_library.py — Level 1 classification of report_library, from the URL only, no download.

Matches the WBA Source Type tokens (the curated multilingual set from tiered-discovery's
verify_types, plus a few strong dictionary phrases) against the full URL path (folders + file
name) of each PDF, and writes the best-scoring Source Type back to report_library, with a
confidence. It resolves about two thirds of the corpus for free; the opaque-URL remainder is left
unlabelled for a Level 2 pass that reads the document content.

Writes, per row:
  source_type        one of the 25 WBA Source Types, or empty when nothing matched
  match_confidence   "high" (a specific multi-word term matched), "low" (only a generic word),
                     or "unresolved" (no token in the URL -> needs content reading)

By default only rows with an empty source_type are processed (so the nightly run only touches the
new PDFs the harvester just added). CLASSIFY_FORCE=true (or --force) re-processes everything.

Modes:
  --source csv --csv <file with a document_url column>   classify offline, print the distribution
  --source airtable                                      read report_library (default); dry-run
  --commit                                               write source_type / match_confidence back

Env (airtable): AIRTABLE_TOKEN, AIRTABLE_BASE, AIRTABLE_LIBRARY_TABLE (default report_library)
Fields to add to report_library: source_type (Single select), match_confidence (Single line text)
"""
import argparse, os, re, sys, time, collections
from urllib.parse import quote, urlsplit

API = "https://api.airtable.com/v0"
from monitor_core import airtable_request
F_ID = "library_id"; F_URL = "document_url"; F_TYPE = "source_type"; F_CONF = "match_confidence"

# Curated, overlapping on purpose (an ESG doc that is really a sustainability report still counts).
# Generic single words are kept only where they are still discriminating for that type.
import json as _json
_TERMS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "source_type_terms.json")
try:
    SOURCE_TYPE_TOKENS = _json.load(open(_TERMS_PATH, encoding="utf-8"))
except Exception:
    SOURCE_TYPE_TOKENS = {"Sustainability Report": ["sustainability report", "sustainab"],
                          "Annual Report": ["annual report"], "Policy Documents": ["policy"]}


OWNERS = {}
for _t, _toks in SOURCE_TYPE_TOKENS.items():
    for _tok in _toks:
        OWNERS.setdefault(_tok, set()).add(_t)


def classify(document_url):
    """Return (source_type, confidence). Whole-token match against the whole URL path,
    so short tokens like 'gri' or 'iso' do not match inside 'nigeria' or 'vision'."""
    path = urlsplit(document_url.split("?", 1)[0]).path.lower()
    text = " " + re.sub(r"\s+", " ",
                        path.replace("-", " ").replace("_", " ").replace("/", " ").replace(".", " ")) + " "
    best_t, best_score, best_specific = None, 0, False
    for t, toks in SOURCE_TYPE_TOKENS.items():
        score = 0; specific = False
        for tok in toks:
            if f" {tok} " in text:
                # a token several types share ("annual report") counts a little less than one only
                # this type has, so ties no longer go to whichever type is listed first in the file
                score += len(tok.split()) - (0.1 if len(OWNERS.get(tok, ())) > 1 else 0)
                if len(tok.split()) >= 2:
                    specific = True
        if score > best_score:
            best_t, best_score, best_specific = t, score, specific
    if not best_t:
        return "", "unresolved"
    return best_t, ("high" if best_specific else "low")


def from_csv(path):
    import csv
    csv.field_size_limit(10_000_000)
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return [r for r in csv.DictReader(fh)]


def from_airtable(base, token, table, force):
    import requests
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}"}
    params = [("pageSize", "100"), ("fields[]", F_ID), ("fields[]", F_URL), ("fields[]", F_CONF)]
    out = []; offset = None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        r = airtable_request("GET", url, headers, params=p); r.raise_for_status()
        j = r.json()
        for rec in j.get("records", []):
            f = rec.get("fields", {})
            # processed rows carry a match_confidence (incl. "unresolved"); skip them unless forced
            if force or not f.get(F_CONF):
                out.append((rec["id"], f.get(F_URL, "")))
        offset = j.get("offset"); time.sleep(0.22)
        if not offset: break
    return out


# ---- Level 1b: label from the downloaded file -------------------------------------------------
# Many sites serve PDFs from addresses with no words in them (AmRest /en/media/download/3162,
# Sitecore .ashx, /document/<id>). The URL pass above leaves those 'unresolved' and without a year,
# so they show as "Company | Document" and company coverage does not count them (AmRest, Oct 2026:
# ~80 reports downloaded, 2025 annual report still "missing"). Once the file is attached and its
# cover text is read, the file name and the title page usually say exactly what the document is:
# "AmRest Group_Condensed consolidated interim report H1 2026.pdf". --from-file classifies those
# rows again from that text. Runs nightly after Build cover text (dedupe workflow).
F_FILE = "file"; F_COVER = "cover_text"; F_YEAR = "doc_year"
YEAR = re.compile(r"(?<!\d)(20[0-3]\d)(?!\d)")
YEAR_PHRASE = re.compile(r"(?:year|period|months|financial year|fiscal year|fy)\s+(?:ended|ending)?\s*"
                         r"(?:on\s+)?(?:\d{1,2}\s+)?(?:[a-z]+\s+)?(?:\d{1,2},?\s+)?(20[0-3]\d)", re.I)


def _name(f):
    from urllib.parse import unquote
    a = (f.get(F_FILE) or [{}])[0]
    return unquote(a.get("filename") or "")


def year_from(filename, cover, this_year=None):
    """Reporting year: from the file name first, else a 'year ended ... 2025' phrase in the cover,
    else the latest plausible year on the title page. '' when nothing credible is found."""
    import datetime as _dt
    this_year = this_year or _dt.date.today().year
    ok = lambda y: 2000 <= y <= this_year + 1
    ys = [int(y) for y in YEAR.findall(filename) if ok(int(y))]
    if ys:
        return str(max(ys))
    m = YEAR_PHRASE.search(cover[:3000])
    if m and ok(int(m.group(1))):
        return m.group(1)
    ys = [int(y) for y in YEAR.findall(cover[:400]) if ok(int(y))]
    return str(max(ys)) if ys else ""


_COVER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cover_terms.json")
try:
    COVER_TERMS = {k: v for k, v in _json.load(open(_COVER_PATH, encoding="utf-8")).items() if not k.startswith("_")}
except Exception:
    COVER_TERMS = {}


def fold(text):
    """Lower case, accents removed, spaces collapsed: 'Relatório  Anual' -> 'relatorio anual'."""
    import unicodedata
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).lower()
    t = t.replace("æ", "ae").replace("ø", "o").replace("å", "a").replace("ß", "ss").replace("ł", "l")
    return re.sub(r"\s+", " ", t)


def _rx(phrase):
    p = fold(phrase)
    if re.search(r"[a-z]", p) and not re.search(r"[^\x00-\x7f]", p.replace("'", "")):
        return re.compile(r"(?<![a-z])" + re.escape(p).replace(r"\ ", r"[\s\-_]*") + r"(?![a-z])")
    return re.compile(re.escape(p).replace(r"\ ", r"\s*"))      # CJK, Thai, Arabic, Cyrillic: no word edges


_COVER_RX = [(t, len(p), _rx(p)) for t, ps in COVER_TERMS.items() for p in ps]


def cover_type(cover, head=600):
    """Type from a cover-title phrase in another language: the phrase that appears first wins, then
    the longer one. '' when none. (Applied only after the address and the file name said nothing.)"""
    text = fold((cover or "")[:head])
    best = None
    for t, n, rx in _COVER_RX:
        m = rx.search(text)
        if m and (best is None or (m.start(), -n) < (best[0], -best[1])):
            best = (m.start(), n, t)
    return best[2] if best else ""


def classify_from_file(filename, cover):
    """(source_type, confidence) from the file name, else from the title page (first 400 chars),
    else from a cover-title phrase in another language (cover_terms.json)."""
    t, c = classify("/" + filename.replace(" ", "-")) if filename else ("", "unresolved")
    if t:
        return t, "file name"
    head = re.sub(r"[^\w]+", "-", cover[:400].lower())
    t, c = classify("/" + head)
    if t:
        return t, "cover"
    t = cover_type(cover)
    return (t, "cover title") if t else ("", "unresolved")


GENERIC = {"download", "downloads", "document", "documents", "file", "files", "view", "pdf", "attachment",
           "media", "getfile", "index", "untitled", "doc", "final", "web", "print", "version", "copy",
           "the", "and", "for", "eng", "english", "low", "res", "high", "online", "new"}
JUNK_TOKEN = re.compile(r"^(?:(?=[0-9a-f]*\d)[0-9a-f]{8,}|[0-9a-f-]{20,}|\d{5,}|v\d+(?:\.\d+)*|\d|"
                        r"en|eng|web|final|print|lowres|hires|copy)$", re.I)
COVER_STOP = re.compile(r"\.{4,}|\s1\.\s+[A-Z]|\btable of contents\b|\bcontents\b|\binhalt\b|\bsommaire\b|\b[ií]ndice\b", re.I)


def _meaningful(words):
    return [w for w in words if len(re.sub(r"[^A-Za-z\u00C0-\u024F\u0400-\u04FF]", "", w)) >= 3
            and w.lower() not in GENERIC]


def title_from(filename, cover):
    """A human-readable title: the file name when it says something ('AmRest Group_Condensed
    consolidated interim report H1 2026.pdf'), else the start of the cover page, cut before the
    table of contents. '' when neither says anything."""
    from urllib.parse import unquote
    stem = re.sub(r"\.pdf$", "", unquote(unquote(filename or "")), flags=re.I)
    words = [w for w in re.sub(r"[_+\-.]+", " ", stem).split() if not JUNK_TOKEN.match(w)]
    if len(_meaningful(words)) >= 2:
        return " ".join(words)[:120].strip()
    text = re.sub(r"\s+", " ", (cover or "")[:700])
    m = COVER_STOP.search(text)
    if m:
        text = text[:m.start()]
    out = []
    for w in text.split()[:18]:
        if not out or w.lower() != out[-1].lower():
            out.append(w)
    for k in range(6, 1, -1):          # a cover that repeats its title ("ANNUAL REPORT 2025 ANNUAL REPORT 2025")
        if len(out) >= 2 * k and [w.lower() for w in out[:k]] == [w.lower() for w in out[k:2 * k]]:
            out = out[:k] + out[2 * k:]
            break
    t = " ".join(out)
    if len(t) > 110:
        t = t[:110].rsplit(" ", 1)[0]
    return t.strip(" -|:,;") if len(_meaningful(t.split())) >= 3 else ""


def from_file_rows(base, token, table, limit=0):
    """Rows with a read cover and no auto title yet (discarded rows skipped)."""
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}"}
    formula = f"AND({{{F_COVER}}}!='', {{auto_title}}='', NOT({{discard}}))"
    params = [("pageSize", "100"), ("filterByFormula", formula)] + \
             [("fields[]", x) for x in (F_ID, F_URL, F_FILE, F_COVER, F_YEAR, F_CONF, F_TYPE)]
    out = []; offset = None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        r = airtable_request("GET", url, headers, params=p); r.raise_for_status()
        j = r.json()
        out += j.get("records", [])
        offset = j.get("offset"); time.sleep(0.22)
        if not offset or (limit and len(out) >= limit): break
    return out[:limit] if limit else out


def label_from_file(records):
    """updates: auto_title on every row (Untitled when nothing readable), doc_year when blank, and
    source_type for rows the URL pass left unresolved. Counters for the log."""
    updates, by = [], collections.Counter()
    for rec in records:
        f = rec.get("fields", {})
        name, cover = _name(f), f.get(F_COVER) or ""
        upd = {"auto_title": title_from(name, cover) or "Untitled"}
        by["titled" if upd["auto_title"] != "Untitled" else "untitled"] += 1
        if not f.get(F_TYPE) or f.get(F_CONF) in (None, "", "unresolved"):
            t, c = classify_from_file(name, cover)
            if t:
                upd[F_TYPE] = t; upd[F_CONF] = c; by["typed"] += 1
        if not (f.get(F_YEAR) or "").strip():
            y = year_from(name, cover)
            if y:
                upd[F_YEAR] = y; by["dated"] += 1
        updates.append({"id": rec["id"], "fields": upd})
    return updates, by


def untyped_rows(base, token, table, limit=0):
    """Rows with a read cover, no address type (or unresolved) and no AI type (discarded rows skipped)."""
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}"}
    formula = (f"AND({{{F_COVER}}}!='', {{source_type_check}}='', OR({{{F_TYPE}}}='', {{{F_CONF}}}='unresolved'), "
               f"NOT({{discard}}))")
    params = [("pageSize", "100"), ("filterByFormula", formula)] + \
             [("fields[]", x) for x in (F_ID, F_URL, F_FILE, F_COVER, F_CONF, F_TYPE)]
    out = []; offset = None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        r = airtable_request("GET", url, headers, params=p); r.raise_for_status()
        j = r.json()
        out += j.get("records", [])
        offset = j.get("offset"); time.sleep(0.22)
        if not offset or (limit and len(out) >= limit): break
    return out[:limit] if limit else out


def retype(records):
    updates, by = [], collections.Counter()
    for rec in records:
        f = rec.get("fields", {})
        t, c = classify_from_file(_name(f), f.get(F_COVER) or "")
        if t:
            updates.append({"id": rec["id"], "fields": {F_TYPE: t, F_CONF: c}})
            by[t] += 1
    return updates, by


def commit(base, token, table, updates):
    import requests
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    print(f"writing source_type/match_confidence on {len(updates)} rows ...")
    for i in range(0, len(updates), 10):
        r = airtable_request("PATCH", url, headers, {"records": updates[i:i + 10], "typecast": True})
        r.raise_for_status(); time.sleep(0.22)
        if i and i % 1000 == 0:
            print(f"  {i}/{len(updates)}")
    print("done.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["airtable", "csv"], default="airtable")
    ap.add_argument("--csv")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-classify rows that already have a type")
    ap.add_argument("--from-file", action="store_true",
                    help="title, date and (if unresolved) type rows from their file name and cover text")
    ap.add_argument("--limit", type=int, default=0, help="--from-file: at most this many rows (0 = all)")
    ap.add_argument("--retype-untyped", action="store_true",
                    help="rows with a cover but no type at all (no address type, no AI type): type them from the "
                         "file name and cover, including cover-title phrases in other languages")
    args = ap.parse_args()
    if args.retype_untyped:
        token = os.environ.get("AIRTABLE_TOKEN"); base = os.environ.get("AIRTABLE_BASE")
        table = os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library")
        if not (token and base):
            sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
        recs = untyped_rows(base, token, table, args.limit)
        updates, by = retype(recs)
        print(f"retype untyped: {len(recs)} rows with a cover and no type; {len(updates)} typed; {dict(by)}")
        if args.commit and updates:
            commit(base, token, table, updates)
        elif not args.commit:
            print("DRY-RUN. Re-run with --commit to write the types.")
        return
    if args.from_file:
        token = os.environ.get("AIRTABLE_TOKEN"); base = os.environ.get("AIRTABLE_BASE")
        table = os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library")
        if not (token and base):
            sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
        recs = from_file_rows(base, token, table, args.limit)
        updates, by = label_from_file(recs)
        print(f"label from file: {len(recs)} rows with cover text and no auto title; {dict(by)}; "
              f"{len(updates)} rows to update.")
        if args.commit and updates:
            commit(base, token, table, updates)
        elif not args.commit:
            print("DRY-RUN. Re-run with --commit to write the labels.")
        return
    force = args.force or os.environ.get("CLASSIFY_FORCE", "").lower() in ("true", "1", "yes")

    by_type = collections.Counter(); by_conf = collections.Counter(); updates = []
    if args.source == "csv":
        if not args.csv:
            sys.exit("--csv PATH required with --source csv")
        for r in from_csv(args.csv):
            t, c = classify(r.get(F_URL, "") or r.get("url", ""))
            by_type[t or "(unresolved)"] += 1; by_conf[c] += 1
    else:
        token = os.environ.get("AIRTABLE_TOKEN"); base = os.environ.get("AIRTABLE_BASE")
        table = os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library")
        if not (token and base):
            sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
        rows = from_airtable(base, token, table, force)
        print(f"{len(rows)} rows to classify{' [FORCE]' if force else ' (missing source_type)'}.")
        for rid, doc in rows:
            t, c = classify(doc)
            by_type[t or "(unresolved)"] += 1; by_conf[c] += 1
            updates.append({"id": rid, "fields": {F_TYPE: t, F_CONF: c}})

    total = sum(by_type.values()) or 1
    print("=" * 56)
    print(f"classified: {total}")
    print(f"  confidence: {dict(by_conf)}")
    resolved = total - by_type.get('(unresolved)', 0)
    print(f"  got a source type: {resolved} ({resolved / total * 100:.1f}%)")
    print("  by source type:")
    for t, n in by_type.most_common():
        print(f"    {t}: {n}")
    print("=" * 56)

    if args.source == "airtable" and args.commit:
        commit(os.environ["AIRTABLE_BASE"], os.environ["AIRTABLE_TOKEN"],
               os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library"), updates)
    elif args.source == "airtable":
        print("\nDRY-RUN. Re-run with --commit to write the labels.")


if __name__ == "__main__":
    main()
