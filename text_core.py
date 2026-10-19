#!/usr/bin/env python3
"""
text_core.py — shared logic of the full-text layer: the page store and the mention matcher.

Two scripts sit on top of it:
    build_fulltext.py   reads every attached PDF in scope and writes its text, page by page, to the store
    mention_search.py   scans the store for terms and reports which companies mention them

Nothing in here talks to Airtable. That keeps the matcher and the store testable offline, and it
is the same split as monitor_core.py: one place for the logic two scripts must agree on.

THE STORE
    Full text does not fit in Airtable (a cell caps at 100,000 characters; one annual report runs
    to a million), so it lives outside, as gzipped JSON-lines files:

        pages-<part>.jsonl.gz   one line per page       {"id": library_id, "p": page, "t": text}
        docs-<part>.jsonl.gz    one line per document   {"id", "rec", "att", "status", "pages", "chars", "at"}
        meta-<run>.jsonl.gz     snapshot of the Airtable metadata of every document in the store
        queue-<run>.json.gz     the work list a planning job hands to the extraction shards

    A "part" is one flush of one extraction job. Parts are append-only and never edited, so a
    killed job loses at most its unflushed tail and a re-run can only add. pages-<part> is uploaded
    BEFORE docs-<part>: the docs file is the done-marker, so a pages file without its docs file is
    an interrupted flush and is ignored. If a document ends up in two parts (re-extraction), the
    newest part wins; part names start with a UTC timestamp, so name order is time order.

    Where the files live is behind two small classes with the same four methods:
        ReleaseStore   a PRIVATE GitHub repository used as a file locker (each file is a release
                       asset). Free, no new vendor, and reachable from Actions with one token.
                       The monitor repo is public, and so are its artifacts and logs, which is
                       why full text is never stored there.
        LocalStore     a folder. Used offline and by the tests.

THE MATCHER
    A search has main terms, optional short names, optional nearby terms and a distance in words.
      - Whole words only, never substrings: "gender" does not match "engendered".
      - Case does not matter, EXCEPT that a term typed in capitals (WBA, CHRB) matches
        capitals only: lower-case "wba" inside URLs and codes is noise. Accents do matter:
        type a term the way it is written.
      - A trailing * matches any ending: benchmark* finds benchmark, benchmarks, benchmarking.
      - A hyphen never hides a word: "bench-" + line break + "marking" matches benchmarking,
        "co-operation" matches cooperation, and "gender-responsive" still matches gender.
      - Short names (acronyms) are ambiguous, so they only count once the document has shown
        which meaning it uses: either a main term appears somewhere in it, or a confirming word
        sits within CONFIRM_DISTANCE words of the acronym ("WBA Social Benchmark"). On the first
        pages of the library, capital WBA without the full name is ours in 8 companies and
        something else in 6 (Wireless Broadband Alliance, Wyoming Business Alliance, Worldwide
        Brewers Association, World Biogas Association): neither "always" nor "never" is right.
      - With nearby terms, a main term or short name counts only when a nearby term sits within
        `distance` words of it on the same page. Both words anywhere in the same document is not
        evidence: "gender" appears in nearly every sustainability report.
"""
import os, re, gzip, json, time, shutil, threading, unicodedata
from concurrent.futures import ThreadPoolExecutor

DEFAULT_DISTANCE = 25        # words allowed between a main term and a nearby term
SNIPPET_CONTEXT = 22         # words of context kept on each side of a match
MAX_SNIPPETS = 3             # evidence passages kept per document
MERGE_GAP = 3                # anchors this close are one mention ("World Benchmarking Alliance (WBA)")
CONFIRM_DISTANCE = 8         # a confirming word this close makes a short name count on its own

OK_STATUS = "ok"
# Outcomes that will not change on a retry: these documents are done. Everything else
# (download_fail, truncated) is transient and is retried on later runs, up to MAX_ATTEMPTS.
TERMINAL = {"ok", "no_text", "encrypted", "parse_fail", "not_pdf", "too_big", "timeout", "crash", "no_file"}
MAX_ATTEMPTS = 3


# ----------------------------------------------------------------------------------------------
# Text cleaning (applied once, at extraction, so the store holds text that is safe to search)
# ----------------------------------------------------------------------------------------------
# NUL and other control characters, the soft hyphen, zero-width characters and the BOM: invisible
# in a viewer, fatal to a literal search. Private-use code points (symbol-font bullets and ticks,
# U+E000-U+F8FF) and the replacement character are glyphs with no text behind them: they show as
# boxes in the evidence and can never be searched for, so they go too. Built with chr() so no
# invisible character sits in this file.
_CTRL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\xad"
                   + chr(0x200B) + "-" + chr(0x200D) + chr(0x2060) + chr(0xFEFF)
                   + chr(0xE000) + "-" + chr(0xF8FF) + chr(0xFFFD) + "]")
_HYPHENS = ("-", chr(0x2010), chr(0x2011))     # hyphen-minus, hyphen, non-breaking hyphen
_SPACES = re.compile(r"[ \t]+")
_NL_PAD = re.compile(r" *\n *")
_NL_RUN = re.compile(r"\n{3,}")
_HYPHEN_BREAK = re.compile(r"(?<=[^\W_])-[ \n](?=[^\W_])")


def clean_page_text(s):
    """Normalise one page of extracted text without losing words.
    NFKC folds the PDF-specific forms that would hide a word from a literal search: ligatures
    (the single glyph "fi" in "financial"), full-width Latin in Japanese reports, non-breaking
    and thin spaces. Soft hyphens and zero-width characters are dropped for the same reason.
    A word hyphenated across a line break ("bench-" / "marking") is pulled back together with
    its hyphen KEPT ("bench-marking"): the matcher reads that as benchmarking, and a real
    compound broken at the same spot ("gender-" / "equality") still shows both of its words.
    After this function a hyphen inside a word is always the one character "-" followed
    directly by the rest of the word, which is the only shape the scanner has to undo."""
    if not s:
        return ""
    s = s.encode("utf-8", "ignore").decode("utf-8")      # lone surrogates from broken encodings
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    for h in _HYPHENS[1:]:                             # one hyphen character from here on
        s = s.replace(h, "-")
    s = _CTRL.sub("", s)
    s = _SPACES.sub(" ", s)
    s = _NL_PAD.sub("\n", s)
    s = _NL_RUN.sub("\n\n", s)
    s = _HYPHEN_BREAK.sub("-", s)
    return s.strip()


# ----------------------------------------------------------------------------------------------
# Tokens
# ----------------------------------------------------------------------------------------------
# A token is a run of letters and digits of ONE script family:
#   - Latin letters (accented ones included) and digits;
#   - any other alphabet (Hangul, Cyrillic, Greek, Arabic, Thai ...);
#   - Chinese and Japanese are written without spaces, so every ideograph / kana is its own token
#     and "distance in words" becomes distance in characters for those scripts.
# Splitting at the script boundary matters: Korean glues its particles onto foreign names
# ("World Benchmarking Alliance" + a Hangul particle, SK Telecom's 2026 report), and with a plain
# \w+ the last word of the name is no longer the word we search for.
def _ranges(*pairs):
    return "".join(f"{chr(a)}-{chr(b)}" for a, b in pairs)


_CJK = _ranges((0x3040, 0x30FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF))
_LATIN = "0-9A-Za-z" + _ranges((0xC0, 0xD6), (0xD8, 0xF6), (0xF8, 0x24F), (0x1E00, 0x1EFF))
_TOKEN = f"[{_CJK}]|[{_LATIN}]+|[^\\W_{_CJK}{_LATIN}]+"
TOKEN_RX = re.compile(_TOKEN)
TERM_RX = re.compile(f"({_TOKEN})(\\*?)")
# Two slots are glued when exactly one hyphen sits between them (clean_page_text has already
# reduced every other shape to this one). The scanner's byte pre-filter deletes the same
# character, so whatever the matcher can glue, the pre-filter sees glued.
GLUE = set(_HYPHENS)
_QUOTES = "\"'" + chr(0x201C) + chr(0x201D) + chr(0x2018) + chr(0x2019)


def fold(s):
    """Case-insensitive form of a word. Accents are kept on purpose: the scanner pre-filters pages
    on raw bytes, and a pre-filter can only be trusted if the matcher never accepts a spelling
    the bytes do not contain."""
    return s.lower()


class Slots:
    """One page as a list of word slots, with the character span of each and a flag saying
    whether a slot is glued to the next one by a hyphen ('bench-marking', 'co-operation')."""
    __slots__ = ("text", "raw", "low", "start", "end", "join", "_ix")

    def __init__(self, text):
        raw, start, end, join = [], [], [], []
        prev = None
        for m in TOKEN_RX.finditer(text):
            s, e = m.span()
            if prev is not None:
                join.append(text[prev:s] in GLUE)
            raw.append(m.group()); start.append(s); end.append(e)
            prev = e
        if raw:
            join.append(False)
        self.text = text; self.raw = raw; self.start = start; self.end = end; self.join = join
        self.low = [fold(t) for t in raw]
        self._ix = {}

    def __len__(self):
        return len(self.raw)

    def index(self, cs):
        """{word form: [slot positions]} including the glued forms of hyphenated words."""
        ix = self._ix.get(cs)
        if ix is None:
            src = self.raw if cs else self.low
            ix = {}
            join = self.join
            for i, t in enumerate(src):
                ix.setdefault(t, []).append(i)
                if join[i]:
                    t2 = t + src[i + 1]
                    ix.setdefault(t2, []).append(i)
                    if join[i + 1]:
                        ix.setdefault(t2 + src[i + 2], []).append(i)
            self._ix[cs] = ix
        return ix


class Term:
    """A search term: one or more words. Each word is (text, case_sensitive, prefix)."""
    __slots__ = ("label", "words")

    def __init__(self, label, words):
        self.label = label; self.words = words


def parse_term(s):
    """'World Benchmarking Alliance' / 'WBA' / 'benchmark*' -> Term, or None for a blank line."""
    s = unicodedata.normalize("NFKC", str(s or "")).strip().strip(_QUOTES).strip()
    words = []
    for m in TERM_RX.finditer(s):
        tok, star = m.group(1), m.group(2)
        cs = len(tok) >= 2 and tok.isupper()          # typed in capitals -> matches capitals only
        words.append((tok if cs else fold(tok), cs, bool(star)))
    return Term(s, words) if words else None


def split_terms(value):
    """A cell or CLI value holding several terms -> list of strings. One term per line; a
    single-line value may separate terms with ';' or '|'. Commas are NOT separators (company
    and index names contain them)."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        parts = [str(v) for v in value]
    else:
        parts = re.split(r"[\n;|]+", str(value))
    return [p.strip() for p in parts if p and p.strip()]


def _match_word(w, cs, prefix, S, i):
    """Slots consumed (1-3) if word w matches at slot i, else 0."""
    src = S.raw if cs else S.low
    t = src[i]
    if (t.startswith(w) if prefix else t == w):
        return 1
    if S.join[i]:
        t = t + src[i + 1]
        if (t.startswith(w) if prefix else t == w):
            return 2
        if S.join[i + 1]:
            t = t + src[i + 2]
            if (t.startswith(w) if prefix else t == w):
                return 3
    return 0


def _match_term(term, S, i):
    n = len(S.raw); j = i
    for w, cs, prefix in term.words:
        if j >= n:
            return 0
        k = _match_word(w, cs, prefix, S, j)
        if not k:
            return 0
        j += k
    return j - i


def find_term(term, S):
    """Every slot span [i, j) where the term occurs on the page."""
    w, cs, prefix = term.words[0]
    ix = S.index(cs)
    if prefix:
        starts = sorted({i for form, pos in ix.items() if form.startswith(w) for i in pos})
    else:
        starts = ix.get(w, ())
    out = []
    for i in starts:
        k = _match_term(term, S, i)
        if k:
            out.append((i, i + k))
    return out


# ----------------------------------------------------------------------------------------------
# Searches
# ----------------------------------------------------------------------------------------------
def _int(v):
    try:
        if v is None or v == "":
            return None
        return int(float(v))
    except (TypeError, ValueError):
        return None


class Query:
    """One search. `key` is its stable id (the Airtable record id of the search row)."""

    def __init__(self, key, name, find, short=None, confirm=None, near=None, distance=None,
                 types=None, year_from=None, year_to=None, companies=None):
        self.key = key; self.name = name or key
        self.main = [t for t in map(parse_term, split_terms(find)) if t]
        self.short = [t for t in map(parse_term, split_terms(short)) if t]
        self.confirm = [t for t in map(parse_term, split_terms(confirm)) if t]
        self.near = [t for t in map(parse_term, split_terms(near)) if t]
        if not self.main:
            raise ValueError(f"search '{self.name}': 'find' needs at least one term")
        d = _int(distance)
        self.distance = DEFAULT_DISTANCE if d is None or d < 0 else d
        self.types = {t.strip() for t in (types or []) if t and str(t).strip()}
        self.year_from = _int(year_from); self.year_to = _int(year_to)
        self.companies = {c for c in (companies or []) if c}

    def in_scope(self, m):
        """Does a document (its meta line) fall inside this search's scope?"""
        if self.types and (m.get("type") or "") not in self.types:
            return False
        if self.companies and (m.get("wba") or "") not in self.companies:
            return False
        if self.year_from is not None or self.year_to is not None:
            y = _int(m.get("year"))
            if y is None:                       # undated documents cannot answer a year question
                return False
            if self.year_from is not None and y < self.year_from:
                return False
            if self.year_to is not None and y > self.year_to:
                return False
        return True

    def needles(self):
        """Cheap pre-filter. One entry per main term and short name: a tuple of lower-case byte
        strings that must ALL be present in a page for that term to possibly occur on it (the
        plain-ASCII core of each word of the term). A page is worth parsing when at least one
        entry is fully present. None as an entry means 'this term cannot be pre-filtered': the
        caller must then look at every page."""
        out = []
        for t in self.main + self.short:
            need = set()
            for w, _, _ in t.words:
                runs = sorted(re.findall(r"[a-z0-9]+", w.lower()), key=len, reverse=True)
                if runs and len(runs[0]) >= 2:
                    need.add(runs[0].encode("ascii"))
            if need:
                out.append(tuple(sorted(need, key=lambda b: (-len(b), b))))      # rarest (longest) first
            elif all(len(w) == 1 and not w.isascii() for w, _, _ in t.words):
                out.append((t.words[0][0].encode("utf-8"),))                     # CJK: first character, no case
            else:
                out.append(None)
        return out

    def describe(self):
        s = " | ".join(t.label for t in self.main)
        if self.short:
            s += "  (+ short names: " + " | ".join(t.label for t in self.short)
            if self.confirm:
                s += ", confirmed by: " + " | ".join(t.label for t in self.confirm)
            s += ")"
        if self.near:
            s += f"  within {self.distance} words of: " + " | ".join(t.label for t in self.near)
        return s


def _gap(ai, aj, bi, bj):
    """Words between two slot spans; 0 when they touch or overlap."""
    return bi - aj if bi >= aj else (ai - bj if bj <= ai else 0)


def match_page(q, S):
    """What one page holds for one search. Returns None when no main term or short name occurs,
    else {"ok": bool, "occ": [...], "spans": [...]}:
        ok     this page settles what the short names mean: a main term occurs on it, or a short
               name stands next to a confirming word (feeds the short-name rule, document level)
        occ    qualifying occurrences: (gap, i, j, label, is_main, ni, nj, near_label)
        spans  every matched span on the page, for highlighting"""
    main = [(i, j, t.label, True) for t in q.main for i, j in find_term(t, S)]
    short = [(i, j, t.label, False) for t in q.short for i, j in find_term(t, S)]
    if not main and not short:
        return None
    ok = bool(main)
    if not ok and q.confirm:
        conf = [span for t in q.confirm for span in find_term(t, S)]
        ok = any(_gap(ai, aj, ci, cj) <= CONFIRM_DISTANCE for ai, aj, _, _ in short for ci, cj in conf)
    anchors = sorted(main + short)
    spans = [(i, j) for i, j, _, _ in anchors]
    occ = []
    if q.near:
        nears = sorted((i, j, t.label) for t in q.near for i, j in find_term(t, S))
        if nears:
            spans += [(i, j) for i, j, _ in nears]
            for ai, aj, lab, is_main in anchors:
                best = None
                for ni, nj, nlab in nears:
                    gap = _gap(ai, aj, ni, nj)
                    if best is None or gap < best[0]:
                        best = (gap, ni, nj, nlab)
                if best[0] <= q.distance:
                    occ.append((best[0], ai, aj, lab, is_main, best[1], best[2], best[3]))
    else:
        occ = [(None, ai, aj, lab, is_main, None, None, None) for ai, aj, lab, is_main in anchors]
    return {"ok": ok, "occ": occ, "spans": spans}


_MD = re.compile(r"[*_`~]")


def _plain(s):
    """Snippet text is shown in a rich-text cell: drop the few characters Markdown would act on."""
    return re.sub(r"\s+", " ", _MD.sub(" ", s)).replace("](", "] (")


def _window(S, lo, hi, spans):
    """Text of slots [lo, hi) with every matched span inside it in **bold**."""
    inside = sorted({(max(i, lo), min(j, hi)) for i, j in spans if j > lo and i < hi})
    merged = []
    for i, j in inside:
        if merged and i < merged[-1][1]:                 # overlapping only: neighbours stay separate
            merged[-1] = (merged[-1][0], max(merged[-1][1], j))
        else:
            merged.append((i, j))
    text = S.text; pos = S.start[lo]; out = []
    for i, j in merged:
        a, b = S.start[i], S.end[j - 1]
        out.append(_plain(text[pos:a]))
        out.append("**" + _plain(text[a:b]).strip() + "**")
        pos = b
    out.append(_plain(text[pos:S.end[hi - 1]]))
    return re.sub(r" +", " ", "".join(out)).strip()


def snippet(S, focus, spans, context=SNIPPET_CONTEXT):
    """The passage around `focus` (one or two slot spans: the term, and the nearby term when
    there is one). When the two sit far apart the middle is cut out, so a snippet stays short
    whatever distance the search allows."""
    n = len(S)
    focus = sorted(focus)
    lo = max(0, focus[0][0] - context); hi = min(n, focus[-1][1] + context)
    if len(focus) == 2 and focus[1][0] - focus[0][1] > 2 * context:
        cut_a = focus[0][1] + context // 2; cut_b = focus[1][0] - context // 2
        body = (_window(S, lo, cut_a, spans) + f" [... {cut_b - cut_a} words ...] " + _window(S, cut_b, hi, spans))
    else:
        body = _window(S, lo, hi, spans)
    return ("… " if lo > 0 else "") + body + (" …" if hi < n else "")


def doc_hit(q, doc_id, pages):
    """Combine one document's matching pages into a single hit, applying the short-name rule.
    `pages` is [(page_no, Slots, match_page result)] in page order. Returns None when nothing
    qualifies, else a plain dict (picklable, it crosses process boundaries)."""
    ours = any(m["ok"] for _, _, m in pages)             # do the short names mean what we mean?
    best = []; mentions = 0; page_nos = []; labels = set(); near_labels = set(); gap_min = None
    for p, S, m in pages:
        occ = [o for o in m["occ"] if o[4] or ours]
        if not occ:
            continue
        occ.sort(key=lambda o: (o[0] if o[0] is not None else 0, o[1]))
        last = None
        for o in sorted(occ, key=lambda o: o[1]):          # count distinct mentions on the page
            if last is None or o[1] - last > MERGE_GAP:
                mentions += 1
            last = o[2]
            labels.add(o[3])
            if o[7]:
                near_labels.add(o[7])
        top = occ[0]
        if top[0] is not None and (gap_min is None or top[0] < gap_min):
            gap_min = top[0]
        page_nos.append(p)
        focus = [(top[1], top[2])] + ([(top[5], top[6])] if top[5] is not None else [])
        best.append((top[0] if top[0] is not None else 0, p, S, focus, m["spans"]))
    if not page_nos:
        return None
    best.sort(key=lambda b: (b[0], b[1]))
    snips = [(p, (g if q.near else None), snippet(S, focus, spans))
             for g, p, S, focus, spans in best[:MAX_SNIPPETS]]
    return {"id": doc_id, "pages": page_nos, "mentions": mentions, "gap": gap_min,
            "snips": snips, "terms": sorted(labels), "near": sorted(near_labels)}


def match_text(q, doc_id, text, page=0):
    """Convenience for single-text documents (cover text): one page, one call."""
    S = Slots(clean_page_text(text))
    m = match_page(q, S)
    return doc_hit(q, doc_id, [(page, S, m)]) if m else None


# ----------------------------------------------------------------------------------------------
# Part files
# ----------------------------------------------------------------------------------------------
def part_name(run, shard, seq):
    return f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{run}-{shard:02d}-{seq:03d}"


def write_jsonl_gz(path, rows):
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(tmp, path)


def read_jsonl_gz(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


class PartWriter:
    """Streams pages to pages-<part>.jsonl.gz on disk and keeps the per-document lines for
    docs-<part>.jsonl.gz. Key order in the page line is fixed (id, p, t): the scanner reads the
    id without parsing the JSON."""

    def __init__(self, folder, part):
        self.part = part
        self.pages_path = os.path.join(folder, f"pages-{part}.jsonl.gz")
        self.docs_path = os.path.join(folder, f"docs-{part}.jsonl.gz")
        self._f = gzip.open(self.pages_path + ".tmp", "wt", encoding="utf-8", compresslevel=6)
        self.docs = []; self.chars = 0; self.pages = 0; self.opened = time.time()

    def add(self, doc, pages):
        """doc: the docs-file line. pages: [(page_no, text)] (empty for failures)."""
        for p, t in pages:
            self._f.write('{"id":' + json.dumps(doc["id"], ensure_ascii=False) + ',"p":' + str(int(p))
                          + ',"t":' + json.dumps(t, ensure_ascii=False) + "}\n")
            self.chars += len(t); self.pages += 1
        self.docs.append(doc)

    def close(self):
        self._f.close()
        os.replace(self.pages_path + ".tmp", self.pages_path)
        write_jsonl_gz(self.docs_path, self.docs)
        return self.pages_path, self.docs_path


def part_of(name):
    """'pages-20261013T101500-r1-00-001.jsonl.gz' -> ('pages', '20261013T101500-r1-00-001')."""
    m = re.match(r"^(pages|docs)-(.+)\.jsonl\.gz$", os.path.basename(name))
    return (m.group(1), m.group(2)) if m else (None, None)


def load_docs_index(folder, names):
    """Read every docs-<part> file -> {library_id: line + {"part", "attempts"}}; the newest part
    wins, and attempts counts how many parts have tried the document."""
    index = {}
    for name in sorted(names):
        kind, part = part_of(name)
        if kind != "docs":
            continue
        for d in read_jsonl_gz(os.path.join(folder, name)):
            prev = index.get(d["id"])
            d["part"] = part
            d["attempts"] = (prev["attempts"] if prev else 0) + 1
            index[d["id"]] = d
    return index


# ----------------------------------------------------------------------------------------------
# Scanning
# ----------------------------------------------------------------------------------------------
_SCAN = {}


def _scan_init(queries, meta, owner):
    _SCAN["q"] = queries; _SCAN["meta"] = meta; _SCAN["owner"] = owner
    needles = set(); every = False
    for q in queries:
        for nd in q.needles():
            if nd is None:
                every = True
            else:
                needles.add(nd)
    # an entry whose words all appear in another entry is the weaker condition: keep only that one
    keep = [n for n in needles if not any(o != n and set(o) < set(n) for o in needles)]
    _SCAN["needles"] = sorted(keep); _SCAN["every"] = every


def _line_id(line):
    """library_id of a page line without parsing the JSON (PartWriter fixes the key order)."""
    if line.startswith(b'{"id":"'):
        k = line.find(b'","p":', 7)
        if k > 0 and b"\\" not in line[7:k]:
            return line[7:k].decode("utf-8")
    return json.loads(line)["id"]


BLOCK = 4 << 20              # bytes of decompressed text handled at a time by the scanner


def scan_part(path):
    """Scan one pages file with every search. Returns [(query index, hit dict)].
    Runs in a worker process; the searches and the metadata arrive through _scan_init.

    Speed comes from never looking closely at text that cannot match. The file is read in blocks
    of a few megabytes; a block is lower-cased and stripped of hyphens in two C-level passes, and
    if no term has all of its words somewhere in the block, the whole block is skipped without
    even being split into lines. Only pages that hold every word of some term are parsed and
    matched properly. Deleting "-" is exactly what the matcher's glue rule can do (see GLUE), so
    the pre-filter can let a page through for nothing but can never hide one."""
    queries = _SCAN["q"]; meta = _SCAN["meta"]; owner = _SCAN["owner"]
    needles = _SCAN["needles"]; every = _SCAN["every"]
    _, part = part_of(path)
    hits = []
    state = {"cur": None, "active": (), "cand": {}}

    def finish():
        for qi, pages in state["cand"].items():
            h = doc_hit(queries[qi], state["cur"], pages)
            if h:
                hits.append((qi, h))
        state["cand"] = {}

    def page(line):
        doc = _line_id(line)
        if doc != state["cur"]:
            if state["cand"]:
                finish()
            state["cur"] = doc
            m = meta.get(doc)
            # a document belongs to the NEWEST part that holds it, and only documents that are
            # still in the metadata snapshot (existing, with a file, not discarded) are searched
            state["active"] = [qi for qi, q in enumerate(queries) if q.in_scope(m)] \
                if (m is not None and owner.get(doc) == part) else ()
        if not state["active"]:
            return
        rec = json.loads(line)
        S = Slots(rec["t"])
        for qi in state["active"]:
            m = match_page(queries[qi], S)
            if m:
                state["cand"].setdefault(qi, []).append((rec["p"], S, m))

    def block(data):
        if every:
            for line in data.split(b"\n"):
                if len(line) >= 12:
                    page(line)
            return
        low = data.lower()
        if b"-" in low:
            low = low.replace(b"-", b"")
        live = [nd for nd in needles if all(w in low for w in nd)]    # terms this block could hold
        if not live:
            return
        lines = data.split(b"\n"); lows = low.split(b"\n")     # same length: no pass touches a line break
        for k, ll in enumerate(lows):
            for nd in live:
                if nd[0] in ll and all(w in ll for w in nd):
                    page(lines[k])
                    break

    with gzip.open(path, "rb") as f:
        tail = b""
        while True:
            data = f.read(BLOCK)
            if not data:
                break
            data = tail + data
            cut = data.rfind(b"\n")
            if cut < 0:
                tail = data
                continue
            tail = data[cut + 1:]
            block(data[:cut])
        if tail.strip():
            block(tail)
    if state["cand"]:
        finish()
    return hits


def scan_store(folder, page_files, queries, meta, owner, workers=None):
    """Scan every pages file in parallel. Returns {query index: {library_id: hit}}."""
    import multiprocessing as mp
    results = {i: {} for i in range(len(queries))}
    paths = [os.path.join(folder, n) for n in sorted(page_files)]
    if not paths:
        return results
    workers = max(1, min(workers or os.cpu_count() or 2, len(paths)))
    if workers == 1:
        _scan_init(queries, meta, owner)
        batches = map(scan_part, paths)
        for hits in batches:
            for qi, h in hits:
                results[qi][h["id"]] = h
        return results
    with mp.Pool(workers, initializer=_scan_init, initargs=(queries, meta, owner)) as pool:
        for hits in pool.imap_unordered(scan_part, paths):
            for qi, h in hits:
                results[qi][h["id"]] = h
    return results


# ----------------------------------------------------------------------------------------------
# Stores
# ----------------------------------------------------------------------------------------------
class LocalStore:
    """A folder of files. Same four methods as ReleaseStore."""

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.label = f"folder {root}"

    def assets(self, refresh=False):
        out = {}
        for n in os.listdir(self.root):
            p = os.path.join(self.root, n)
            if os.path.isfile(p) and not n.endswith(".tmp"):
                out[n] = {"name": n, "size": os.path.getsize(p)}
        return out

    def upload(self, path, name=None):
        name = name or os.path.basename(path)
        tmp = os.path.join(self.root, name + ".tmp")
        shutil.copyfile(path, tmp)
        os.replace(tmp, os.path.join(self.root, name))

    def download(self, name, dest):
        shutil.copyfile(os.path.join(self.root, name), dest + ".tmp")
        os.replace(dest + ".tmp", dest)

    def delete(self, name):
        try:
            os.remove(os.path.join(self.root, name))
        except FileNotFoundError:
            pass


class StoreError(RuntimeError):
    pass


class StoreUnavailable(StoreError):
    """No store is configured, or it holds no text yet: an expected state, not a failure."""


class ReleaseStore:
    """A private GitHub repository used as a file locker. Each file is a release asset.

    GitHub allows 1,000 assets per release. Part files therefore go into numbered releases
    (text-parts-0001, text-parts-0002 ...), and a new one is started once the current one holds
    FILES_PER_RELEASE files; shards uploading at the same moment can overshoot that by two files
    each, which is what the margin below the real cap is for. Everything else (metadata
    snapshots, queues) sits in text-state, where old files are pruned.
    The token needs Contents read+write on that one repository and nothing else. The repository
    must contain at least one commit (create it with a README): GitHub cannot tag an empty repo."""

    API = "https://api.github.com"
    UPLOADS = "https://uploads.github.com"
    FILES_PER_RELEASE = 800
    STATE_TAG = "text-state"
    PARTS_TAG = "text-parts-%04d"
    # GitHub's secondary rate limit is 900 "points" a minute (a GET costs 1, a write 5). Spacing
    # the calls keeps a search that downloads a thousand small parts in parallel under it.
    MIN_INTERVAL = 0.1
    _gate = threading.Lock()
    _last_call = [0.0]

    def __init__(self, repo, token, session=None):
        import requests
        self.repo = repo.strip().strip("/")
        self.s = session or requests.Session()
        self.h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                  "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "wba-link-monitor-text-store"}
        self._assets = None; self._releases = {}
        self._parts_no = None; self._part_rel = (None, None)
        self.label = f"github.com/{self.repo} (releases)"

    # -- plumbing ---------------------------------------------------------------------------
    def _req(self, method, url, ok=(200,), tries=5, **kw):
        """One GitHub call with backoff on network errors, 5xx and rate limits. Returns the
        response for any status in `ok`; 404 and 422 are returned too (callers interpret them);
        anything else raises StoreError with GitHub's message."""
        import requests
        headers = dict(self.h); headers.update(kw.pop("headers", {}) or {})
        kw.setdefault("timeout", 120)
        delay = 2; r = None
        for attempt in range(1, tries + 1):
            with self._gate:                              # one pace for every thread
                wait = self._last_call[0] + self.MIN_INTERVAL - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last_call[0] = time.monotonic()
            try:
                r = self.s.request(method, url, headers=headers, **kw)
            except requests.exceptions.RequestException as e:
                if attempt == tries:
                    raise StoreError(f"GitHub {method} {url}: {type(e).__name__}: {e}")
                time.sleep(delay); delay = min(delay * 2, 60); continue
            if r.status_code in ok or r.status_code in (404, 422):
                return r
            limited = r.status_code == 429 or (r.status_code == 403 and (
                r.headers.get("Retry-After") or r.headers.get("X-RateLimit-Remaining") == "0"
                or "rate limit" in r.text.lower()))
            if (r.status_code >= 500 or limited) and attempt < tries:
                wait = r.headers.get("Retry-After")
                time.sleep(min(int(wait), 120) if wait and wait.isdigit() else delay)
                delay = min(delay * 2, 60); continue
            hint = ""
            if r.status_code in (401, 403) and not limited:
                hint = ("  -> check the TEXT_STORE_TOKEN secret: a fine-grained token with "
                        "Contents read and write on this repository only.")
            raise StoreError(f"GitHub {method} {url}: HTTP {r.status_code} {r.text[:300]}{hint}")
        return r

    def _paged(self, url):
        page = 1
        while True:
            r = self._req("GET", url, params={"per_page": 100, "page": page})
            if r.status_code == 404:
                raise StoreError(f"GitHub: {self.repo} not found or not visible to the token. "
                                 f"Check TEXT_STORE_REPO (owner/name) and the token's repository access.")
            rows = r.json()
            yield from rows
            if len(rows) < 100:
                return
            page += 1

    def _parts_release(self):
        """The numbered release with room for one more part. Counted fresh every time: other
        shards upload to the same release while this one works."""
        if self._parts_no is None:
            nos = [int(m.group(1)) for rel in self._paged(f"{self.API}/repos/{self.repo}/releases")
                   for m in [re.match(r"^text-parts-(\d+)$", rel.get("tag_name") or "")] if m]
            self._parts_no = max(nos) if nos else 1
        while True:
            rel = self._release(self.PARTS_TAG % self._parts_no, create=True)
            held = sum(1 for _ in self._paged(f"{self.API}/repos/{self.repo}/releases/{rel['id']}/assets"))
            if held < self.FILES_PER_RELEASE:
                return rel
            self._parts_no += 1

    def _release(self, tag, create=False):
        if tag in self._releases:
            return self._releases[tag]
        r = self._req("GET", f"{self.API}/repos/{self.repo}/releases/tags/{tag}")
        if r.status_code == 404 and create:
            r = self._req("POST", f"{self.API}/repos/{self.repo}/releases", ok=(201,),
                          json={"tag_name": tag, "name": tag,
                                "body": "Full-text store written by wba-link-monitor. Do not edit by hand."})
            if r.status_code == 422:                      # empty repo, or another shard just created it
                again = self._req("GET", f"{self.API}/repos/{self.repo}/releases/tags/{tag}")
                if again.status_code == 200:
                    r = again
                else:
                    raise StoreError(f"GitHub could not create release {tag} in {self.repo}: {r.text[:300]}  "
                                     f"-> if the repository is empty, add a README so it has a first commit.")
        if r.status_code not in (200, 201):
            if create:
                raise StoreError(f"GitHub: no release {tag} in {self.repo} and it could not be created "
                                 f"(HTTP {r.status_code}). Check TEXT_STORE_REPO and the token's access.")
            return None
        rel = r.json()
        self._releases[tag] = rel
        return rel

    # -- the four methods ---------------------------------------------------------------------
    def assets(self, refresh=False):
        """{name: {"name", "size", "id", "tag"}} for every COMPLETE asset in the text-* releases."""
        if self._assets is not None and not refresh:
            return self._assets
        out = {}
        for rel in self._paged(f"{self.API}/repos/{self.repo}/releases"):
            tag = rel.get("tag_name") or ""
            if not tag.startswith("text-"):
                continue
            self._releases[tag] = rel
            for a in self._paged(f"{self.API}/repos/{self.repo}/releases/{rel['id']}/assets"):
                if a.get("state") != "uploaded":         # a half-finished upload is not a file
                    continue
                out[a["name"]] = {"name": a["name"], "size": a["size"], "id": a["id"], "tag": tag}
        self._assets = out
        return out

    def upload(self, path, name=None):
        name = name or os.path.basename(path)
        kind, part = part_of(name)
        if kind is None:
            rel = self._release(self.STATE_TAG, create=True)
        elif self._part_rel[0] == part:                   # the two files of a part stay together
            rel = self._part_rel[1]
        else:
            rel = self._parts_release()
            self._part_rel = (part, rel)
        size = os.path.getsize(path)
        why = ""
        for attempt in range(1, 5):
            url = f"{self.UPLOADS}/repos/{self.repo}/releases/{rel['id']}/assets"
            r = None
            try:
                # tries=1: the body is a file handle, so a retry must reopen it, which this loop does
                with open(path, "rb") as f:
                    r = self._req("POST", url, ok=(201,), tries=1, params={"name": name}, data=f, timeout=900,
                                  headers={"Content-Type": "application/gzip", "Content-Length": str(size)})
            except StoreError as e:
                why = str(e)
            if r is not None and r.status_code == 201:
                a = r.json()
                if self._assets is not None:
                    self._assets[name] = {"name": name, "size": a.get("size", size), "id": a["id"],
                                          "tag": rel["tag_name"]}
                return
            if r is not None:
                why = f"HTTP {r.status_code} {r.text[:300]}"
                if r.status_code == 422 and "file_count" in r.text and kind is not None:
                    # the release is full by GitHub's own count (1,000 assets): start the next one
                    self._parts_no = (self._parts_no or 1) + 1
                    rel = self._parts_release()
                    self._part_rel = (part, rel)
                    continue
            # The send failed or GitHub answered 422 (an asset with this name exists: an earlier
            # attempt, complete or not). A complete asset of the same size IS this file; anything
            # else under this name is a broken leftover and is removed before the next attempt.
            old = None
            for a in self._paged(f"{self.API}/repos/{self.repo}/releases/{rel['id']}/assets"):
                if a["name"] == name:
                    old = a
            if old and old.get("state") == "uploaded" and old.get("size") == size:
                if self._assets is not None:
                    self._assets[name] = {"name": name, "size": size, "id": old["id"], "tag": rel["tag_name"]}
                return
            if old:
                self._req("DELETE", f"{self.API}/repos/{self.repo}/releases/assets/{old['id']}", ok=(204,))
            time.sleep(2 * attempt)
        raise StoreError(f"GitHub: could not upload {name} after 4 attempts: {why}")

    def download(self, name, dest):
        a = self.assets().get(name)
        if not a:
            raise StoreError(f"store has no file named {name}")
        url = f"{self.API}/repos/{self.repo}/releases/assets/{a['id']}"
        last = None
        for attempt in range(1, 5):
            try:
                r = self._req("GET", url, headers={"Accept": "application/octet-stream"}, stream=True, timeout=300)
                if r.status_code != 200:
                    raise StoreError(f"HTTP {r.status_code}")
                with open(dest + ".tmp", "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                got = os.path.getsize(dest + ".tmp")
                if got != a["size"]:
                    raise StoreError(f"size {got} != {a['size']}")
                os.replace(dest + ".tmp", dest)
                return
            except Exception as e:                        # incomplete body, dropped connection
                last = e
                time.sleep(3 * attempt)
        raise StoreError(f"GitHub: could not download {name}: {last}")

    def delete(self, name):
        a = self.assets().get(name)
        if a:
            self._req("DELETE", f"{self.API}/repos/{self.repo}/releases/assets/{a['id']}", ok=(204,))
            self._assets.pop(name, None)


def open_store():
    """The store named by the environment: TEXT_STORE_REPO + TEXT_STORE_TOKEN (private GitHub
    repository) or TEXT_STORE_DIR (a folder)."""
    repo = (os.environ.get("TEXT_STORE_REPO") or "").strip()
    token = (os.environ.get("TEXT_STORE_TOKEN") or "").strip()
    folder = (os.environ.get("TEXT_STORE_DIR") or "").strip()
    if repo and token:
        return ReleaseStore(repo, token)
    if folder:
        return LocalStore(folder)
    raise StoreUnavailable(
        "No text store configured. Set TEXT_STORE_REPO (owner/name of a PRIVATE repository) and "
        "TEXT_STORE_TOKEN (fine-grained token, Contents read and write on that repository), or "
        "TEXT_STORE_DIR for a local folder. See TEXT_SEARCH.md.")


def sync_down(store, names, folder, workers=8):
    """Download the named files into `folder`, skipping those already there with the right size."""
    os.makedirs(folder, exist_ok=True)
    listing = store.assets()
    todo = []
    for n in names:
        p = os.path.join(folder, n)
        if not (os.path.exists(p) and os.path.getsize(p) == listing[n]["size"]):
            todo.append(n)
    if todo:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(lambda n: store.download(n, os.path.join(folder, n)), todo))
    return len(todo)


def complete_parts(listing):
    """Names of the docs files and of the pages files that are safe to read: a pages file counts
    only when its docs file (the done-marker, uploaded second) is present too."""
    docs = {part_of(n)[1]: n for n in listing if part_of(n)[0] == "docs"}
    pages = {part_of(n)[1]: n for n in listing if part_of(n)[0] == "pages"}
    return sorted(docs.values()), sorted(n for part, n in pages.items() if part in docs)
