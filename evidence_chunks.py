#!/usr/bin/env python3
"""
evidence_chunks.py: cut the full text of the reports into passages and tag each passage as evidence on
climate, nature, social or financial data, by measurement area (planning, implementation, performance) and integration
aspect, following the grid of WBA's Integrated Transition Assessment white paper (September 2026,
Appendix D, Table D.1). Owner's request, 24 Oct 2026: a first step towards a later information-extraction
tool; nothing here scores a company.

Scope: documents in the full-text store whose type is a report of any kind (sustainability family, annual,
integrated, 10-K, registration document, financial statement, interim report, presentation, single-topic
report) with year 2024 or later, not discarded, not yet tagged with the current
lexicon version (or every one with --all).

Passages: each page is split into sentences and the sentences are grouped into passages of about
TARGET_WORDS words (never across pages), so a passage can be quoted with its page. A passage is kept when it
holds at least min_terms distinct phrases of one dimension (evidence_terms.json); boilerplate that names one
term in passing is not evidence.

Tags per kept passage:
    dims        the dimensions it is evidence for (climate / nature / social), with the topics hit
    area        planning / implementation / performance: the area whose cue words it holds most; a
                passage with a quantity (%, tCO2e, MWh, m3, hectares, a rate) leans to performance
    integration the integration aspects named (strategy coverage, integrated governance, cross-dimension,
                value chain, AI and digital)
    number      True when it states a quantity

Output, in the private text store (the full text never goes to Airtable or to this public repository):
    evidence-<run>.jsonl.gz       one line per passage {id, p, n, dims, topics, area, integration, number, t}
    evidence-docs-<run>.jsonl.gz  one line per document {id, v, passages, by_dim, by_area}
and on report_library: evidence_profile (counts in words, e.g. "climate 84 (planning 30, implementation 32,
performance 22) · nature 12 · social 41") and evidence_version (the lexicon version that produced it).

Dry run by default: tags a sample of --limit documents and prints counts and example passages (no upload,
no write). Env: AIRTABLE_TOKEN, AIRTABLE_BASE, TEXT_STORE_REPO + TEXT_STORE_TOKEN (or TEXT_STORE_DIR).
"""
import argparse, collections, gzip, hashlib, json, os, re, tempfile, time

import qa
from monitor_core import airtable_request

API = "https://api.airtable.com/v0"
TYPES = {"Sustainability Report", "ESG Report", "CSR Report", "Integrated Report", "Impact Report", "Annual Report",
         "10K Form", "Registration Document", "Financial Statement", "Interim Reports", "Company Presentations",
         "Climate Report", "Environmental Reports", "Social Reports", "CDP Report", "GRI", "SASB Index"}
YEAR_MIN = 2024
TARGET_WORDS = 120
MAX_CHARS = 1800
TERMS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence_terms.json")
NUMBER = re.compile(r"\d[\d,.]*\s?(?:%|per\s?cent|t\s?CO2e?|tCO2e?|tonnes?|tons?|kt|Mt|MWh|GWh|TWh|GJ|TJ|m3|m³|ML|megalitres?|"
                    r"hectares?|ha\b|km2|employees|injur|fatalit)", re.I)


def load_terms(path=TERMS_FILE):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def version(terms):
    return hashlib.sha1(json.dumps(terms, sort_keys=True).encode()).hexdigest()[:8]


def _rx(phrases):
    """(case-insensitive regex, capitals-only regex) for a list of phrases; None when the list is empty."""
    ci, cs = [], []
    for ph in phrases:
        star = ph.endswith("*")
        core = ph[:-1] if star else ph
        body = re.escape(core).replace(r"\ ", r"[\s-]+") + (r"\w*" if star else "")
        letters = [c for c in core if c.isalpha()]
        (cs if letters and all(c.isupper() for c in letters) and len(letters) >= 2 else ci).append(body)
    mk = lambda xs, fl: re.compile(r"(?<!\w)(?:" + "|".join(sorted(xs, key=len, reverse=True)) + r")(?!\w)", fl) if xs else None
    return mk(ci, re.I), mk(cs, 0)


class Lexicon:
    def __init__(self, terms):
        self.min_terms = int(terms.get("min_terms", 3))
        self.min_terms_two_topics = int(terms.get("min_terms_two_topics", 2))
        self.dims = {d: {t: _rx(ph) for t, ph in topics.items()} for d, topics in terms["dimensions"].items()}
        self.areas = {a: _rx(ph) for a, ph in terms["areas"].items()}
        self.integration = {k: _rx(ph) for k, ph in terms["integration"].items()}

    @staticmethod
    def hits(pair, text):
        """Distinct phrases found, normalised so a line break, a hyphen or a plural is not a second phrase."""
        out = {}
        for rx in pair:
            if rx:
                for m in rx.finditer(text):
                    out.setdefault(norm_hit(m.group(0)), re.sub(r"\s+", " ", m.group(0).lower()))
        return out

    def tag(self, text):
        """The tags of one passage, or None when it is evidence for no dimension (or it is a contents list)."""
        if is_contents(text):
            return None
        dims, topics = {}, {}
        for d, tops in self.dims.items():
            found = {}
            for t, pair in tops.items():
                h = self.hits(pair, text)
                if h:
                    topics.setdefault(d, []).append(t)
                    found.update(h)
            # three distinct phrases, or two from two different topics: "diversity and inclusion" in a
            # proxy-advisor paragraph, or "EU Taxonomy" named twice, is not evidence (24 Oct 2026 sample)
            if len(found) >= self.min_terms or (len(found) >= self.min_terms_two_topics and len(topics.get(d, [])) >= 2):
                dims[d] = sorted(set(found.values()))[:12]
        if not dims:
            return None
        number = bool(NUMBER.search(text))
        score = {a: len(self.hits(pair, text)) for a, pair in self.areas.items()}
        if number:
            score["performance"] = score.get("performance", 0) + 2
        area = max(score, key=lambda a: (score[a], a == "performance")) if any(score.values()) else "unclear"
        integ = sorted(k for k, pair in self.integration.items() if self.hits(pair, text))
        return {"dims": sorted(dims), "terms": dims, "topics": {d: topics[d] for d in dims}, "area": area,
                "integration": integ, "number": number}


CONTENTS_ENTRY = re.compile(r"(?<![\d.,%])\b\d{1,3}\s+[A-Z][a-z]")


def is_contents(text):
    """A table of contents: many 'Title 32 Next title 33' entries and hardly any full sentence."""
    return len(CONTENTS_ENTRY.findall(text)) >= 6 and text.count(". ") <= 2


def norm_hit(s):
    s = re.sub(r"[\s-]+", " ", s.lower()).strip()
    return s[:-1] if len(s) > 4 and s.endswith("s") and not s.endswith("ss") else s


def strip_repeated_lines(pages, share=0.3, min_count=3):
    """Drop lines that repeat on many pages of the same document (running headers, navigation tabs,
    footers), so a passage starts with what the page says, not with its menu."""
    count = collections.Counter()
    for _, text in pages:
        count.update({re.sub(r"\s+", " ", ln).strip().lower() for ln in (text or "").splitlines() if ln.strip()})
    limit = max(min_count, int(len(pages) * share))
    rep = {ln for ln, c in count.items() if c >= limit and len(ln) < 120}
    out = []
    for p, text in pages:
        keep = [ln for ln in (text or "").splitlines() if re.sub(r"\s+", " ", ln).strip().lower() not in rep]
        out.append((p, "\n".join(keep)))
    return out


SENT = re.compile(r"(?<=[.!?;])\s+(?=[A-Z0-9•\-–(])")


def passages(text, target=TARGET_WORDS):
    """Split one page into passages of about `target` words, by sentences."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    out, cur, n = [], [], 0
    for s in SENT.split(text):
        w = len(s.split())
        cur.append(s); n += w
        if n >= target:
            out.append(" ".join(cur)[:MAX_CHARS]); cur, n = [], 0
    if cur:
        if out and n < target // 3:
            out[-1] = (out[-1] + " " + " ".join(cur))[:MAX_CHARS]
        else:
            out.append(" ".join(cur)[:MAX_CHARS])
    return out


def tag_document(lex, lid, pages):
    """[passage lines] and the document summary for one document's pages [(page, text)]."""
    lines, by_dim, by_area = [], collections.Counter(), collections.defaultdict(collections.Counter)
    for p, text in strip_repeated_lines(sorted(pages)):
        for n, t in enumerate(passages(text)):
            tg = lex.tag(t)
            if not tg:
                continue
            lines.append({"id": lid, "p": p, "n": n, **tg, "t": t})
            for d in tg["dims"]:
                by_dim[d] += 1
                by_area[d][tg["area"]] += 1
    return lines, {"id": lid, "passages": len(lines), "by_dim": dict(by_dim), "by_area": {d: dict(v) for d, v in by_area.items()}}


def profile(summary):
    parts = []
    for d in ("climate", "nature", "social", "financial"):
        n = summary["by_dim"].get(d, 0)
        if n:
            a = summary["by_area"].get(d, {}) if d != "financial" else {}
            inner = ", ".join(f"{k} {a[k]}" for k in ("planning", "implementation", "performance") if a.get(k))
            parts.append(f"{d} {n}" + (f" ({inner})" if inner else ""))
    return " · ".join(parts) or "no climate, nature, social or financial passages found"


def rows(token, base, ver, everything):
    H = {"Authorization": f"Bearer {token}"}
    todo = "" if everything else f", {{evidence_version}}!='{ver}'"
    formula = f"AND(NOT({{discard}}), {{year}}>={YEAR_MIN}{todo})"
    params = [("pageSize", "100"), ("filterByFormula", formula)] + \
             [("fields[]", x) for x in ("library_id", "source_type_check", "source_type", "evidence_version")]
    out, offset = {}, None
    while True:
        p = params + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", f"{API}/{base}/report_library", H, params=p).json()
        for r in j.get("records", []):
            f = r["fields"]
            t = (f.get("source_type_check") or "").strip() or (f.get("source_type") or "").strip()
            if f.get("library_id") and t in TYPES:
                out[f["library_id"]] = r
        offset = j.get("offset")
        if not offset:
            return out
        time.sleep(0.2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--all", action="store_true", help="re-tag every document, not only new ones")
    ap.add_argument("--limit", type=int, default=0, help="documents this run (dry run default: 40)")
    a = ap.parse_args()
    token, base = os.environ["AIRTABLE_TOKEN"], os.environ["AIRTABLE_BASE"]
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    limit = a.limit or (0 if a.commit else 40)
    terms = load_terms()
    ver, lex = version(terms), Lexicon(terms)
    run = qa.Run("evidence_chunks.py")
    lib = rows(token, base, ver, a.all)

    import text_core as tc
    store = tc.open_store()
    folder = tempfile.mkdtemp(prefix="ev-")
    names = [n for n in store.assets() if tc.part_of(n)[0]]
    tc.sync_down(store, [n for n in names if n.startswith("docs-")], folder)
    index = tc.load_docs_index(folder, names)
    want = collections.defaultdict(set)
    picked = 0
    for lid in lib:
        d = index.get(lid)
        if d and d.get("status") == "ok":
            want[d["part"]].add(lid); picked += 1
            if limit and picked >= limit:
                break
    print(f">>> {len(lib):,} documents in scope, {picked:,} with full text to tag (lexicon {ver})", flush=True)

    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    ev_path = os.path.join(folder, f"evidence-{stamp}.jsonl.gz")
    docs, totals, areas, integ, samples = [], collections.Counter(), collections.Counter(), collections.Counter(), collections.defaultdict(list)
    with gzip.open(ev_path, "wt", encoding="utf-8") as out:
        for part, ids in want.items():
            name = f"pages-{part}.jsonl.gz"
            tc.sync_down(store, [name], folder)
            pages = collections.defaultdict(list)
            for r in tc.read_jsonl_gz(os.path.join(folder, name)):
                if r["id"] in ids and index[r["id"]]["part"] == part:
                    pages[r["id"]].append((r["p"], r.get("t") or ""))
            os.remove(os.path.join(folder, name))
            for lid, pg in pages.items():
                lines, summary = tag_document(lex, lid, pg)
                summary["v"] = ver
                docs.append(summary)
                for ln in lines:
                    out.write(json.dumps(ln, ensure_ascii=False, separators=(",", ":")) + "\n")
                    for d in ln["dims"]:
                        totals[d] += 1
                        if len(samples[(d, ln["area"])]) < 3:
                            samples[(d, ln["area"])].append(f"{lid} p.{ln['p']} [{', '.join(ln['terms'][d][:5])}]: {ln['t'][:400]}")
                    areas[ln["area"]] += 1
                    integ.update(ln["integration"])
    n_pass = sum(d["passages"] for d in docs)
    md = ["### Evidence passages (climate, nature, social)" + ("" if a.commit else " (dry run, sample)"), "",
          f"Lexicon {ver}. Documents tagged: {len(docs):,}; passages kept: {n_pass:,} "
          f"({n_pass / (len(docs) or 1):.0f} a document).", "",
          "By dimension: " + ", ".join(f"{k} {v:,}" for k, v in totals.most_common()) + ".",
          "By area: " + ", ".join(f"{k} {v:,}" for k, v in areas.most_common()) + ".",
          "Integration aspects named: " + (", ".join(f"{k} {v:,}" for k, v in integ.most_common()) or "none") + ".", ""]
    for d in ("climate", "nature", "social", "financial"):
        for ar in ("planning", "implementation", "performance"):
            for s in samples.get((d, ar), []):
                md.append(f"- **{d} / {ar}** {s}")
    md.append("")
    open("evidence_chunks.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)

    if a.commit and docs:
        dpath = os.path.join(folder, f"evidence-docs-{stamp}.jsonl.gz")
        tc.write_jsonl_gz(dpath, docs)
        store.upload(ev_path)                 # passages first: the docs file is the done-marker
        store.upload(dpath)
        H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        ups = [{"id": lib[d["id"]]["id"], "fields": {"evidence_profile": profile(d), "evidence_version": ver}} for d in docs]
        for i in range(0, len(ups), 10):
            airtable_request("PATCH", f"{API}/{base}/report_library", H, {"records": ups[i:i + 10]})
            time.sleep(0.22)
    run.finish("ok", rows_in=len(lib), rows_out=len(docs), counters={"passages": n_pass, "by_dim": dict(totals), "by_area": dict(areas)})
    run.write_step_summary("ok", dict(totals), title="Evidence passages")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        open("evidence_chunks.md", "w").write("### The run failed\n\n```\n" + traceback.format_exc()[-3000:] + "\n```\n")
        raise
