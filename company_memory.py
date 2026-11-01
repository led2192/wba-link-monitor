#!/usr/bin/env python3
"""
company_memory.py: what a company's right answer for another year says about where to look this year.

Companies print a figure the same way from one report to the next: the same line ("Direct GHG emissions
(Scope 1)"), the same kind of document, the same scale. When the right value for another year is known
(a validated answer, or later an accepted row), the pages of that year's documents that print it show the
line and the scale. That memory is then used for the year being read, in two places:

  1. retrieval: pages of this year's documents that carry the remembered line join the pool and get
     MEMORY_WEIGHT in the page score (extract_eval.page_score);
  2. the model reader is told, as a hint, how the company printed the figure that other year (never the
     value), and still has to find this year's column.

The year being read is never used: its own answer is what the reader is scored against. Read-only.
"""
import re

from value_eval import found_on, printed_forms, scales_for

MEMORY_WEIGHT = 1.0                  # page score added to a page carrying the remembered line
MEMORY_POOL = 20                     # pages of this year's documents the remembered line may bring in
MIN_SHARE = 0.8                      # share of the line's words a page must carry
STOP = {"the", "and", "for", "of", "in", "to", "a", "an", "on", "by", "per", "total", "group"}
SCALE_WORDS = {1: "units", 1e3: "thousands", 1e4: "ten thousands", 1e6: "millions", 1e8: "hundred millions", 1e9: "billions"}


def line_words(line):
    """The words of a table line without its numbers, units in brackets kept ("(tCO2e)" -> "tco2e")."""
    return [w for w in re.findall(r"[a-z][a-z0-9\-]+", line.lower()) if w not in STOP and not re.fullmatch(r"fy\d+", w)]


def label_of(body, printed):
    """The line that prints the number, as words: the words on its own line, or, when the line holds only
    numbers (a table read column by column), the nearest line above with words. "" when none."""
    lines = (body or "").splitlines()
    for i, line in enumerate(lines):
        if found_on(line, printed):
            ws = line_words(line)
            j = i
            while len(ws) < 2 and j > 0 and i - j < 3:
                j -= 1
                ws = line_words(lines[j]) + ws
            return " ".join(ws[:10])
    return ""


def memory_for(cur, wba, attr, year, known, year_documents):
    """{"year", "library_id", "page", "doc_type", "label", "scale"} from the known right value nearest to
    `year` (never `year` itself), or None. known: {year: value}. year_documents: extract_eval's function."""
    if attr.get("memory") is False:          # switched off for the attribute in attributes.json
        return None
    ranks = {t: i for i, t in enumerate(attr.get("doc_types", []))}
    for y in sorted((y for y in known if y != year and known[y]), key=lambda y: (abs(y - year), -y)):
        value = known[y]
        docs, _ = year_documents(cur, wba, y, attr)
        docs = [d for d in docs if d[2] == y] or docs
        if not docs:
            continue
        forms = printed_forms(value, scales_for(attr.get("unit_family")))
        if not forms:
            continue
        from value_eval import tsquery_for
        q = " | ".join(f"({tsquery_for(v, d)})" for v, _, d in forms)
        cur.execute("select library_id, page, body from public.pages where library_id = any(%s) "
                    "and words @@ to_tsquery('simple', %s) limit 40", ([d[0] for d in docs], q))
        dtype = {d[0]: d[1] for d in docs}
        best = None
        for lid, page, body in cur.fetchall():
            for v, _, _ in forms:
                if not found_on(body, v):
                    continue
                label = label_of(body, v)
                if len(label.split()) < 2:
                    continue
                key = (ranks.get(dtype.get(lid), 99), page)
                if best is None or key < best[0]:
                    best = (key, {"year": y, "library_id": lid, "page": page, "doc_type": dtype.get(lid, ""),
                                  "label": label, "scale": round(value / v) if v else 1})
                break
        if best:
            return best[1]
    return None


def share(body, memory):
    """Share of the remembered line's words the page carries."""
    ws = set((memory or {}).get("label", "").split())
    if not ws:
        return 0.0
    have = set(line_words(body or ""))
    return len(ws & have) / len(ws)


def boost(body, memory):
    """1.0 when the page carries the remembered line (MIN_SHARE of its words), else 0."""
    return 1.0 if memory and share(body, memory) >= MIN_SHARE else 0.0


def memory_pages(cur, ids, memory, have):
    """[(library_id, page, body, 0.0)] of this year's pages that carry the remembered line and are not in
    the pool yet. The company's pages first (the word index is not on library_id: left to itself it matches
    common words across the whole store, 31 Oct 2026), then the line's words with AND."""
    ws = sorted(set((memory or {}).get("label", "").split()))
    if not ws:
        return []
    q = " & ".join(re.sub(r"[^a-z0-9]", "", w) for w in ws if re.sub(r"[^a-z0-9]", "", w))
    if not q:
        return []
    cur.execute("with p as materialized (select library_id, page, words from public.pages where library_id = any(%s)) "
                "select p.library_id, p.page, b.body from p join public.pages b using (library_id, page) "
                "where p.words @@ to_tsquery('simple', %s) limit %s", (ids, q, MEMORY_POOL * 3))
    out = []
    for lid, page, body in cur.fetchall():
        if (lid, page) not in have and boost(body, memory):
            out.append((lid, page, body, 0.0))
        if len(out) >= MEMORY_POOL:
            break
    return out


def hint(memory):
    """The line the model reader is told: how the company printed the figure another year, without the value."""
    if not memory:
        return ""
    scale = SCALE_WORDS.get(memory.get("scale"), f"x{memory.get('scale')}")
    where = f"its {memory['year']} {memory['doc_type']}".strip() if memory.get("doc_type") else f"its {memory['year']} report"
    return (f"Hint from another year: in {where} this company printed the figure on a line reading \"{memory['label']}\" "
            f"(words only, numbers removed), in {scale}. The same line in these pages is likely the one; take the column "
            f"for the reporting year asked, and check the scale this page states.")
