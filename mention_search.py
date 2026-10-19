#!/usr/bin/env python3
"""
mention_search.py — which companies mention X in their documents, with the evidence.

A search is a row in `mention_searches`: the names to look for, optional acronyms, optional
nearby words and a distance. This script runs the searches over the full-text store built by
build_fulltext.py (every page of every document in scope) and writes, per search:

    mention_hits        one row per document that mentions it: pages, closest distance, up to three
                        passages with the matched words in bold, a link that opens the PDF on the
                        right page. Reviewers mark each row Mention / Not a mention.
    mention_searches    the headline on the search row: companies found, companies searched, share.

The matching rules (whole words, capitals-only acronyms, hyphens, the short-name rule, distance)
are in text_core.py and are the same for every search.

How to read the share. The denominator is the companies that HAVE a searchable document in the
search's scope, never the whole universe: a company with no document in the store, or only
scanned ones, cannot be said not to mention something. With a company list on the search
(`companies`, one wba_id per line) the result is counted inside that list, which is how
"% of the companies in benchmark B that reference B" is answered.

Where it looks (`look_in` on the search row, or --look-in):
    Full text     the store. The real answer.
    First pages   cover_text straight from Airtable (first six pages, 10,000 characters). Works
                  with no store at all, in about a minute, and is a lower bound only. Its
                  denominator is every company with a document on file, whatever the types and
                  years of the search: Airtable does the reading, so which documents were in
                  scope is not known here.
    A full-text search run before the store exists is reported as "Not run" on its row; the
    first-pages searches of the same run are unaffected.

What a re-run does to existing hit rows. A hit is keyed by search + document, so re-runs update
rows in place and never duplicate. `review` and `notes` belong to people and are never written.
A row that the same kind of run no longer finds is ticked `stale`, never deleted, and comes back
(stale unticked, review intact) if it matches again. A first-pages run never judges a row that
full text produced.

Modes:
    (no flag)          dry run: run the searches, print the headline and the evidence, write nothing
    --commit           write hits and headline numbers to Airtable
Which searches:
    (default)          every search with `active` ticked
    --search NAME      one saved search, by name
    --find ...         an ad hoc search from the command line (--short, --confirm, --near,
                       --distance, --name). With --commit it is saved as a search row first.

Env: AIRTABLE_TOKEN, AIRTABLE_BASE,
     AIRTABLE_SEARCHES_TABLE (default mention_searches), AIRTABLE_HITS_TABLE (default mention_hits),
     AIRTABLE_LIBRARY_TABLE (default report_library), AIRTABLE_COMPANIES_TABLE (default companies),
     TEXT_STORE_REPO + TEXT_STORE_TOKEN, or TEXT_STORE_DIR (full-text searches only)
"""
import os, re, sys, time, argparse, collections, tempfile
from urllib.parse import quote

import text_core as tc

API = "https://api.airtable.com/v0"
SEARCHES = os.environ.get("AIRTABLE_SEARCHES_TABLE", "mention_searches")
HITS = os.environ.get("AIRTABLE_HITS_TABLE", "mention_hits")
LIB = os.environ.get("AIRTABLE_LIBRARY_TABLE", "report_library")
COMP = os.environ.get("AIRTABLE_COMPANIES_TABLE", "companies")

# mention_searches: filled in by people
S_NAME = "search"; S_FIND = "find"; S_SHORT = "short_names"; S_CONFIRM = "confirming_words"
S_NEAR = "near"; S_DIST = "distance"; S_TYPES = "doc_types"; S_YFROM = "year_from"; S_YTO = "year_to"
S_COMP = "companies"; S_LOOK = "look_in"; S_ACTIVE = "active"
# mention_searches: written by this script
S_STATUS = "status"; S_RUN = "last_run"; S_RESULT = "result"
S_CF = "companies_found"; S_CS = "companies_searched"; S_DF = "docs_found"; S_DS = "docs_searched"
SEARCH_FIELDS = [S_NAME, S_FIND, S_SHORT, S_CONFIRM, S_NEAR, S_DIST, S_TYPES, S_YFROM, S_YTO, S_COMP, S_LOOK, S_ACTIVE]

# mention_hits: written by this script (review and notes are people's and are never touched)
H_ID = "hit_id"; H_SEARCH = "search"; H_CO = "company_name"; H_WBA = "wba_id"; H_DOC = "document"
H_YEAR = "year"; H_TYPE = "doc_type"; H_PAGES = "pages"; H_MENTIONS = "mentions"; H_DIST = "distance"
H_EVID = "evidence"; H_OPEN = "open"; H_LIB = "library_id"; H_STALE = "stale"
DIFF_FIELDS = [H_CO, H_WBA, H_DOC, H_YEAR, H_TYPE, H_PAGES, H_MENTIONS, H_DIST, H_OPEN, H_STALE]

# report_library fields read in first-pages mode
L_ID = "library_id"; L_WBA = "wba_id"; L_NAME = "company_name"; L_TYPE_AI = "source_type_check"
L_TYPE_KW = "source_type"; L_YEAR = "year"; L_LABEL = "doc_label"; L_URL = "document_url"
L_COVER = "cover_text"; L_DISCARD = "discard"; L_PAGES = "page_count"

LOOK_TEXT = "Full text"; LOOK_COVER = "First pages"
COVER_PAGES = "first pages"   # what the `pages` cell says on a row found in first-pages mode
MAX_HITS = 5000              # more matching documents than this is a search that needs narrowing
COVER_MAX_ROWS = 25_000      # first-pages mode downloads cover_text; stop when a term is this common
WBA_ID = re.compile(r"^[A-Z]{2,4}_\d{3,}$")
WRITE_BATCH = 10


# ----------------------------------------------------------------------------------------------
# Airtable
# ----------------------------------------------------------------------------------------------
class Airtable:
    def __init__(self):
        from monitor_core import airtable_request      # the retrying helper every script shares
        self.request = airtable_request
        self.token = os.environ.get("AIRTABLE_TOKEN"); self.base = os.environ.get("AIRTABLE_BASE")
        if not (self.token and self.base):
            sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
        self.h = {"Authorization": f"Bearer {self.token}"}

    def url(self, table):
        return f"{API}/{self.base}/{quote(table)}"

    def sweep(self, table, fields, formula=None, cap=None):
        params = [("pageSize", "100")] + [("fields[]", f) for f in fields]
        if formula:
            params.append(("filterByFormula", formula))
        offset = None; n = 0
        while True:
            p = list(params) + ([("offset", offset)] if offset else [])
            j = self.request("GET", self.url(table), self.h, params=p).json()
            for rec in j.get("records", []):
                n += 1
                yield rec
            offset = j.get("offset")
            if not offset or (cap and n >= cap):
                return
            time.sleep(0.12)

    def patch(self, table, records, upsert_on=None):
        """PATCH in chunks of 10. Returns how many rows could not be written; a failed chunk is
        logged and skipped so one bad batch never kills the run (the next run repairs it)."""
        failed = 0
        for i in range(0, len(records), WRITE_BATCH):
            payload = {"records": records[i:i + WRITE_BATCH]}
            if upsert_on:
                payload["performUpsert"] = {"fieldsToMergeOn": [upsert_on]}
            try:
                self.request("PATCH", self.url(table), self.h, payload)
            except Exception as e:
                failed += len(payload["records"])
                print(f">>> WARN: write to {table} failed after retries ({e}); the next run repairs it.", flush=True)
            time.sleep(0.12)
        return failed


def _esc(s):
    return str(s).replace("\\", "\\\\").replace("'", "\\'")


# ----------------------------------------------------------------------------------------------
# Searches
# ----------------------------------------------------------------------------------------------
def company_list(value):
    """The `companies` cell -> (set of wba_ids, entries that are not wba_ids)."""
    ids, junk = set(), []
    for tok in re.split(r"[\s,;|]+", str(value or "")):
        tok = tok.strip()
        if not tok:
            continue
        (ids.add(tok.upper()) if WBA_ID.match(tok.upper()) else junk.append(tok))
    return ids, junk


def query_from_row(rec):
    """A mention_searches record -> (Query, look_in, notes). Raises ValueError on an unusable row."""
    f = rec.get("fields", {})
    ids, junk = company_list(f.get(S_COMP))
    q = tc.Query(rec["id"], (f.get(S_NAME) or "").strip() or rec["id"], f.get(S_FIND),
                 short=f.get(S_SHORT), confirm=f.get(S_CONFIRM), near=f.get(S_NEAR),
                 distance=f.get(S_DIST), types=f.get(S_TYPES) or [], year_from=f.get(S_YFROM),
                 year_to=f.get(S_YTO), companies=ids)
    look = LOOK_COVER if (f.get(S_LOOK) or "").strip().lower().startswith("first") else LOOK_TEXT
    notes = [f"{len(junk)} entr{'y' if len(junk) == 1 else 'ies'} in companies ignored (not a wba_id): "
             + ", ".join(junk[:5])] if junk else []
    return q, look, notes


def load_searches(at, name):
    try:
        rows = list(at.sweep(SEARCHES, SEARCH_FIELDS))
    except Exception:
        print(f">>> {SEARCHES} needs these fields (exact names): " + ", ".join(SEARCH_FIELDS)
              + ", " + ", ".join([S_STATUS, S_RUN, S_RESULT, S_CF, S_CS, S_DF, S_DS])
              + f"\n>>> {HITS} needs: " + ", ".join([H_ID, H_SEARCH, H_LIB] + DIFF_FIELDS + [H_EVID])
              + ", review, notes. See TEXT_SEARCH.md.", flush=True)
        raise
    if name:
        rows = [r for r in rows if (r.get("fields", {}).get(S_NAME) or "").strip().lower() == name.strip().lower()]
        if not rows:
            sys.exit(f"No search named '{name}' in {SEARCHES}.")
    else:
        rows = [r for r in rows if r.get("fields", {}).get(S_ACTIVE)]
    return rows


# ----------------------------------------------------------------------------------------------
# Running: full text
# ----------------------------------------------------------------------------------------------
class TextSource:
    """The store, downloaded once per run and scanned once for all full-text searches."""

    def __init__(self, work, workers):
        self.work = work; self.workers = workers
        self.store = tc.open_store()
        listing = self.store.assets(refresh=True)
        docs_files, page_files = tc.complete_parts(listing)
        metas = sorted(n for n in listing if n.startswith("meta-"))
        if not metas or not docs_files:
            raise tc.StoreUnavailable(f"The text store ({self.store.label}) is empty. Run 'Build full text' "
                                      f"with commit first, or use first-pages mode.")
        tc.sync_down(self.store, docs_files + [metas[-1]], work)
        self.index = tc.load_docs_index(work, docs_files)
        self.meta = {m["id"]: m for m in tc.read_jsonl_gz(os.path.join(work, metas[-1]))}
        # a document is searchable when its newest extraction is ok AND it is still in scope
        self.owner = {i: d["part"] for i, d in self.index.items() if d["status"] == tc.OK_STATUS and i in self.meta}
        parts = set(self.owner.values())
        self.page_files = [n for n in page_files if tc.part_of(n)[1] in parts]
        self.listing = listing
        print(f">>> store {self.store.label}: {len(self.owner):,} searchable documents of "
              f"{len({self.meta[i]['wba'] for i in self.owner}):,} companies, "
              f"{sum(self.index[i].get('text_pages', 0) for i in self.owner):,} pages, "
              f"{len(self.page_files)} part(s), metadata {metas[-1]}", flush=True)

    def run(self, queries):
        t0 = time.time()
        got = tc.sync_down(self.store, self.page_files, self.work)
        size = sum(self.listing[n]["size"] for n in self.page_files) / 1e6
        print(f"    parts ready ({got} downloaded, {size:,.0f} MB compressed) in {time.time() - t0:.0f}s", flush=True)
        t0 = time.time()
        res = tc.scan_store(self.work, self.page_files, queries, self.meta, self.owner, self.workers)
        print(f"    scanned in {time.time() - t0:.0f}s", flush=True)
        out = []
        for qi, q in enumerate(queries):
            scope = [i for i in self.owner if q.in_scope(self.meta[i])]
            unread = sum(1 for i, m in self.meta.items() if i not in self.owner and q.in_scope(m))
            out.append({"hits": res[qi], "searched": set(scope),
                        "companies_searched": {self.meta[i]["wba"] for i in scope if self.meta[i]["wba"]},
                        "pages": sum(self.index[i].get("text_pages", 0) for i in scope),
                        "unread": unread, "meta": self.meta, "look": LOOK_TEXT})
        return out


# ----------------------------------------------------------------------------------------------
# Running: first pages
# ----------------------------------------------------------------------------------------------
def run_cover(at, q, companies):
    """One search over cover_text. Airtable does the coarse filter (documents whose first pages
    contain every word of some term); the real matching happens here, same rules as full text.
    The coarse filter runs on cover_text as stored, where a word hyphenated across a line break
    reads "bench- marking": such a mention is missed here and found by full text. One more
    reason first-pages results are a lower bound."""
    needles = q.needles()
    if any(n is None for n in needles):
        raise ValueError("first-pages mode cannot pre-filter one of the terms (no plain letters or digits "
                         "in it); use full text for this search")

    def find(word):
        return f"FIND('{_esc(word.decode('utf-8'))}', LOWER({{{L_COVER}}}))"

    terms = [find(n[0]) if len(n) == 1 else "AND(" + ",".join(find(w) for w in n) + ")" for n in sorted(set(needles))]
    formula = f"AND(NOT({{{L_DISCARD}}}), {{{L_COVER}}} != '', OR(" + ",".join(terms) + "))"
    fields = [L_ID, L_WBA, L_NAME, L_TYPE_AI, L_TYPE_KW, L_YEAR, L_LABEL, L_URL, L_COVER, L_PAGES]
    hits, meta, searched, n = {}, {}, set(), 0
    for rec in at.sweep(LIB, fields, formula, cap=COVER_MAX_ROWS):
        n += 1
        f = rec.get("fields", {})
        lid = (f.get(L_ID) or "").strip()
        if not lid:
            continue
        y = f.get(L_YEAR)
        try:
            y = int(float(y))
        except (TypeError, ValueError):
            y = ""
        m = {"id": lid, "rec": rec["id"], "wba": (f.get(L_WBA) or "").strip(), "co": f.get(L_NAME) or "",
             "type": (f.get(L_TYPE_AI) or "").strip() or (f.get(L_TYPE_KW) or "").strip(), "year": y,
             "label": f.get(L_LABEL) or "", "url": f.get(L_URL) or "", "pages": f.get(L_PAGES) or 0}
        if not q.in_scope(m):
            continue
        meta[lid] = m; searched.add(lid)
        h = tc.match_text(q, lid, f.get(L_COVER) or "")
        if h:
            hits[lid] = h
    if n >= COVER_MAX_ROWS:
        raise ValueError(f"more than {COVER_MAX_ROWS:,} documents contain one of the terms on their first "
                         f"pages; too common for first-pages mode. Use full text, or more specific terms.")
    pool = {w for w, c in companies.items() if c["files"] > 0 and (not q.companies or w in q.companies)}
    return {"hits": hits, "searched": searched, "companies_searched": pool, "pages": 0, "unread": 0,
            "meta": meta, "look": LOOK_COVER}


def load_companies(at):
    out = {}
    for rec in at.sweep(COMP, ["wba_id", "company_name", "docs_with_file"]):
        f = rec.get("fields", {})
        w = (f.get("wba_id") or "").strip()
        if w:
            out[w] = {"name": f.get("company_name") or "", "files": int(f.get("docs_with_file") or 0)}
    return out


# ----------------------------------------------------------------------------------------------
# Results
# ----------------------------------------------------------------------------------------------
def pct(a, b):
    return f"{100 * a / b:.1f}%" if b else "n/a"


def headline(q, r):
    """Numbers and the one-line result for a search."""
    meta = r["meta"]
    found = {meta[i]["wba"] for i in r["hits"] if meta[i]["wba"]}
    pool = r["companies_searched"]
    found &= pool if pool else found
    cover = r["look"] == LOOK_COVER
    # Full text knows which companies have a document it could read. First pages only knows which
    # companies have a document on file, and says so.
    have = "documents on file" if cover else "searchable documents"
    first = " · first pages only (lower bound)" if cover else ""
    if q.companies:
        missing = len(q.companies - pool)
        text = (f"{len(found):,} of {len(pool):,} listed companies with {have} "
                f"({pct(len(found), len(pool))})"
                + (f"; {missing:,} of the {len(q.companies):,} listed have none" if missing else "") + first)
    else:
        text = f"{len(found):,} of {len(pool):,} companies with {have} ({pct(len(found), len(pool))})" + first
    # In first-pages mode Airtable does the reading, so the number of documents looked at is not
    # known here: `searched` holds only the ones that came back as candidates.
    return {"found": len(found), "pool": len(pool), "docs_found": len(r["hits"]),
            "docs_searched": None if cover else len(r["searched"]), "candidates": len(r["searched"]),
            "text": text}


def page_url(url, page):
    """Open a PDF on the page of the mention. '#page=N' is honoured by every browser viewer."""
    if not url or not page:
        return url or ""
    return url.split("#", 1)[0] + f"#page={int(page)}"


def md_url(url):
    """A URL that is safe inside a Markdown link: brackets and spaces would end the link early."""
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def evidence(h, m, look):
    """The evidence cell (rich text): one paragraph per passage, closest first."""
    lines = []
    for p, gap, text in h["snips"]:
        if look == LOOK_COVER:
            label = "**first pages**"
        else:
            u = page_url(m.get("url"), p)
            label = f"**[p. {p}]({md_url(u)})**" if u.startswith("http") else f"**p. {p}**"
        if gap is not None:
            label += f" ({gap} word{'' if gap == 1 else 's'} apart)"
        lines.append(f"{label}: {text}")
    more = h["pages"][len(h["snips"]):]
    if look != LOOK_COVER and more:
        shown = ", ".join(str(p) for p in more[:25]) + (" ..." if len(more) > 25 else "")
        lines.append(f"Also on p. {shown}")
    return "\n\n".join(lines)


def hit_fields(q, h, m, look):
    pages = COVER_PAGES if look == LOOK_COVER else \
        ", ".join(str(p) for p in h["pages"][:40]) + (" ..." if len(h["pages"]) > 40 else "")
    first_page = None if look == LOOK_COVER else (h["snips"][0][0] if h["snips"] else None)
    return {H_ID: f"{q.key}|{h['id']}", H_SEARCH: [q.key], H_CO: m.get("co") or "", H_WBA: m.get("wba") or "",
            H_LIB: h["id"], H_DOC: (m.get("label") or h["id"])[:250], H_YEAR: m.get("year") or None,
            H_TYPE: m.get("type") or "", H_PAGES: pages, H_MENTIONS: h["mentions"],
            H_DIST: h["gap"], H_EVID: evidence(h, m, look), H_OPEN: page_url(m.get("url"), first_page) or None,
            H_STALE: False}


def _norm(v):
    if v is None or v is False:
        return None
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        return v.strip() or None
    return v


def write_hits(at, q, r, rewrite=False):
    """Upsert this search's hits, tick `stale` on the ones that no longer match. Rows are only
    written when something on them changed, so `last modified` in Airtable stays meaningful.
    The evidence text is not part of that comparison (Airtable hands rich text back re-serialised);
    --rewrite forces every row to be written again, for when only the passages changed."""
    cover_run = r["look"] == LOOK_COVER
    existing = {}
    formula = f"FIND('{_esc(q.key)}|', {{{H_ID}}}) = 1"
    for rec in at.sweep(HITS, [H_ID] + DIFF_FIELDS, formula):
        existing[rec["fields"].get(H_ID)] = rec

    def from_cover(rec):
        return (rec["fields"].get(H_PAGES) or "") == COVER_PAGES

    new_rows, stale_rows, same = [], [], 0
    for lid, h in r["hits"].items():
        fields = hit_fields(q, h, r["meta"][lid], r["look"])
        old = existing.get(fields[H_ID])
        if old and cover_run and not from_cover(old):
            same += 1                                     # full-text evidence is never overwritten by first pages
            continue
        if old and not rewrite and all(_norm(old["fields"].get(k)) == _norm(fields.get(k)) for k in DIFF_FIELDS):
            same += 1
            continue
        new_rows.append({"fields": fields})
    for hid, rec in existing.items():
        lid = (hid or "").split("|", 1)[-1]
        if lid in r["hits"] or rec["fields"].get(H_STALE):
            continue
        # A row goes stale when the kind of run that produced it no longer finds it: a full-text
        # row after a full-text run (the document changed, was discarded, or the search was
        # tightened), a first-pages row after a first-pages run. A first-pages row also goes stale
        # once full text has read that very document and found nothing. A full-text row is never
        # judged by a first-pages run, which sees six pages of it.
        if from_cover(rec) == cover_run or (from_cover(rec) and lid in r["searched"]):
            stale_rows.append({"id": rec["id"], "fields": {H_STALE: True}})
    failed = at.patch(HITS, new_rows, upsert_on=H_ID) + at.patch(HITS, stale_rows)
    return len(new_rows), len(stale_rows), same, failed


def print_search(q, r, head, limit=12):
    print(f"\n=== {q.name}\n    {q.describe()}\n    {head['text']}")
    print((f"    documents: {head['docs_found']:,} of {head['docs_searched']:,} searched"
           if head["docs_searched"] is not None else
           f"    documents: {head['docs_found']:,} ({head['candidates']:,} hold the words on their first pages)")
          + (f", {r['pages']:,} pages" if r["pages"] else "")
          + (f" | {r['unread']:,} in scope have no searchable text" if r["unread"] else ""))
    meta = r["meta"]
    by_co = collections.defaultdict(list)
    for lid, h in r["hits"].items():
        by_co[meta[lid]["co"] or meta[lid]["wba"]].append(h)
    rank = sorted(by_co.items(), key=lambda kv: (min((h["gap"] if h["gap"] is not None else 0) for h in kv[1]), kv[0]))
    for co, hs in rank[:limit]:
        best = min(hs, key=lambda h: (h["gap"] if h["gap"] is not None else 0))
        p, gap, text = best["snips"][0]
        where = "first pages" if r["look"] == LOOK_COVER else f"p. {p}"
        print(f"  - {co} ({len(hs)} doc{'s' if len(hs) != 1 else ''}) | {meta[best['id']]['label'][:60]} | {where}"
              + (f" | {gap} apart" if gap is not None else ""))
        print(f"      {text[:330]}")
    if len(rank) > limit:
        print(f"    ... and {len(rank) - limit} more companies")


def step_summary(blocks):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for q, r, head, note in blocks:
            f.write(f"### {q.name}\n\n**{head['text']}**\n\n`{q.describe()}`\n\n")
            if note:
                f.write(note + "\n\n")
            meta = r["meta"]
            by_co = collections.Counter(meta[i]["co"] or meta[i]["wba"] for i in r["hits"])
            if by_co:
                f.write("| company | documents |\n|---|---|\n")
                for co, n in sorted(by_co.items())[:60]:
                    f.write(f"| {co.replace('|', '/')} | {n} |\n")
                if len(by_co) > 60:
                    f.write(f"\n... and {len(by_co) - 60} more companies.\n")
            f.write("\n")


# ----------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write hits and headline numbers to Airtable")
    ap.add_argument("--search", default="", help="run one saved search, by name (default: all active)")
    ap.add_argument("--find", default="", help="ad hoc search: main terms, separated by ';'")
    ap.add_argument("--short", default="", help="ad hoc: short names / acronyms")
    ap.add_argument("--confirm", default="", help="ad hoc: words that confirm a short name")
    ap.add_argument("--near", default="", help="ad hoc: nearby terms")
    ap.add_argument("--distance", default="", help="ad hoc: words allowed in between (default 25)")
    ap.add_argument("--name", default="", help="ad hoc: name to save the search under (needed with --commit)")
    ap.add_argument("--look-in", default="", choices=["", "text", "cover"],
                    help="override where to look: text = the store, cover = first pages from Airtable")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--max-hits", type=int, default=MAX_HITS)
    ap.add_argument("--rewrite", action="store_true", help="rewrite every hit row, even unchanged ones")
    args = ap.parse_args()
    override = {"text": LOOK_TEXT, "cover": LOOK_COVER}.get(args.look_in)

    at = None
    jobs = []                                             # (Query, look_in, notes)
    if args.find.strip():
        if args.commit and not args.name.strip():
            sys.exit("An ad hoc search needs --name to be saved with --commit.")
        key = "adhoc"
        if args.commit:
            at = Airtable()
            fields = {S_NAME: args.name.strip(), S_FIND: "\n".join(tc.split_terms(args.find)),
                      S_SHORT: "\n".join(tc.split_terms(args.short)) or None,
                      S_CONFIRM: "\n".join(tc.split_terms(args.confirm)) or None,
                      S_NEAR: "\n".join(tc.split_terms(args.near)) or None,
                      S_DIST: int(args.distance) if args.distance.strip() else None}
            r = at.request("PATCH", at.url(SEARCHES), at.h,
                           {"records": [{"fields": fields}], "performUpsert": {"fieldsToMergeOn": [S_NAME]}})
            key = r.json()["records"][0]["id"]
        q = tc.Query(key, args.name.strip() or "ad hoc search", args.find, short=args.short,
                     confirm=args.confirm, near=args.near, distance=args.distance)
        jobs.append((q, override or LOOK_TEXT, []))
    else:
        at = Airtable()
        for rec in load_searches(at, args.search):
            try:
                q, look, notes = query_from_row(rec)
                jobs.append((q, override or look, notes))
            except ValueError as e:
                print(f">>> SKIP {rec.get('fields', {}).get(S_NAME) or rec['id']}: {e}", flush=True)
                if args.commit:
                    at.patch(SEARCHES, [{"id": rec["id"], "fields": {S_STATUS: f"Not run: {e}"[:500]}}])
        if not jobs:
            print(">>> no search to run (tick `active` on a row of mention_searches, or pass --search / --find).")
            return

    text_jobs = [j for j in jobs if j[1] == LOOK_TEXT]
    cover_jobs = [j for j in jobs if j[1] == LOOK_COVER]
    results = {}                                          # query key -> result or Exception
    if text_jobs:
        work = os.environ.get("TEXT_WORK_DIR") or tempfile.mkdtemp(prefix="mentions_")
        os.makedirs(work, exist_ok=True)
        try:
            src = TextSource(work, args.workers)
        except tc.StoreUnavailable as e:
            # No store yet. The full-text searches are reported as not run (and told why on their
            # row); the first-pages searches still run.
            for q, _, _ in text_jobs:
                results[q.key] = e
        else:
            for (q, _, _), r in zip(text_jobs, src.run([j[0] for j in text_jobs])):
                results[q.key] = r
    if cover_jobs:
        at = at or Airtable()
        companies = load_companies(at)
        for q, _, _ in cover_jobs:
            try:
                results[q.key] = run_cover(at, q, companies)
            except ValueError as e:
                results[q.key] = e

    stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    blocks = []
    for q, look, notes in jobs:
        r = results[q.key]
        if isinstance(r, Exception):
            print(f"\n=== {q.name}\n    NOT RUN: {r}", flush=True)
            if args.commit:
                at.patch(SEARCHES, [{"id": q.key, "fields": {S_STATUS: f"Not run: {r}"[:500], S_RUN: stamp}}])
            continue
        head = headline(q, r)
        print_search(q, r, head)
        status = "ok"
        if head["docs_found"] > args.max_hits:
            status = (f"Too broad: {head['docs_found']:,} documents match, over the limit of {args.max_hits:,}. "
                      f"Add nearby terms or narrow the scope. Numbers are shown, documents were not written.")
        if r["unread"]:
            notes = notes + [f"{r['unread']:,} documents in scope have no searchable text (scanned, unreadable "
                             f"or not extracted yet)"]
        note = "; ".join(notes)
        blocks.append((q, r, head, (status if status != "ok" else "") + (" " + note if note else "")))
        if not args.commit:
            continue
        wrote = marked = same = failed = 0
        if status == "ok":
            wrote, marked, same, failed = write_hits(at, q, r, args.rewrite)
        fields = {S_STATUS: (status + ("; " + note if note else ""))[:900], S_RUN: stamp, S_RESULT: head["text"],
                  S_CF: head["found"], S_CS: head["pool"], S_DF: head["docs_found"], S_DS: head["docs_searched"]}
        failed += at.patch(SEARCHES, [{"id": q.key, "fields": fields}])
        print(f"    Airtable: {wrote} hit rows written, {same} unchanged, {marked} ticked stale"
              + (f", {failed} FAILED" if failed else ""), flush=True)
    step_summary(blocks)
    if not args.commit:
        print("\n>>> dry run, nothing written. Re-run with --commit to put the hits in Airtable.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except tc.StoreError as e:
        sys.exit(f">>> STORE ERROR: {e}")
