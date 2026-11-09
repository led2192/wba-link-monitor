#!/usr/bin/env python3
"""
doc_map.py: a map of each document, drawn once and used by every attribute.

Finding the page is where the number reader loses most (the right pages given: +7 to +13 points), and the
search today runs on the attribute's hand-written labels: without them (held-out attributes, 9 Nov 2026)
the figure was on the ranked pages for 18% of answers against 66% to 74% with them. An analyst does not
search words: she opens the contents, goes to "Consolidated statement of cash flows" or "Key figures"
and looks. This module gives the pipeline that map: for every page of the key documents, one line, written
once by a model, saying what the page holds (the section or table, the metrics, the years), stored in
public.page_map with its words and, when the embedding library is installed, a multilingual sentence
embedding. Any attribute, including the 2026 ones with no labels, then finds pages by what they are about
(map_pages): the attribute's name and definition against the lines, by words and by meaning.

    python doc_map.py build [--doc-types "Annual Report,Sustainability Report,..."] [--companies gold|PT_x,PT_y]
                            [--years 2023] [--max-docs N] [--max-usd D] [--commit]
    python doc_map.py embed [--commit]            fill embeddings for lines without one
    python doc_map.py status                      how much of the key documents is mapped

Dry run by default: the lines are written to the database only with --commit. Idempotent: a page already
mapped is never summarised again (upsert on library_id, page). Cost: about 12 pages per model call, 1,400
characters of each page; at the Haiku list price about $0.00008 a page, so the key documents of the 400
companies with validated answers (about 250,000 pages) cost about $20 and the whole library's key documents
about $300. Read-only on everything but public.page_map.
"""
import argparse, collections, concurrent.futures, json, os, re, sys, time

KEY_TYPES = ["Annual Report", "Integrated Report", "Sustainability Report", "ESG Report", "CSR Report", "Climate Report",
             "10K Form", "Financial Statement", "Registration Document", "Code of Conduct", "Human Rights Policy", "Policy"]
PAGES_PER_CALL = 12
PAGE_CHARS = 1400
YEAR_MIN = 2023
EMBED_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"        # 384 dimensions, the model page_select.py already uses
EMBED_DIMS = 384
THREADS = 6
SYSTEM = ("You index the pages of company reports (annual, sustainability, integrated reports, codes and policies) so that "
          "a later search can find the page that holds a given figure or disclosure. You see the first part of each page. "
          "Reply with JSON only.")


def page_prompt(pages):
    """pages: [(page number, text)]. One line per page, at most 20 words, naming what the page holds."""
    parts = [f"=== page {p} ===\n{(t or '')[:PAGE_CHARS]}" for p, t in pages]
    return ("For each page below write one line in English, at most 20 words, saying what the page holds: the section or "
            "table name as the report prints it, the metrics or topics reported (name the figures' subjects, not their values), "
            "and the years shown. If a page is a table of contents, an index, a cover or empty, say so in two or three words. "
            "Reply with one JSON object mapping the page number (as a string) to its line.\n\n" + "\n\n".join(parts))


def parse_lines(text, pages):
    """{page: line} from the model's reply for these pages; pages the reply lacks get no line (never invented)."""
    m = re.search(r"\{.*\}", text or "", flags=re.S)
    if not m:
        return {}
    try:
        j = json.loads(m.group(0))
    except ValueError:
        return {}
    out = {}
    want = {p for p, _ in pages}
    for k, v in (j.items() if isinstance(j, dict) else []):
        try:
            p = int(str(k).strip().replace("page", "").strip())
        except ValueError:
            continue
        line = re.sub(r"\s+", " ", str(v)).strip()
        if p in want and line:
            out[p] = line[:200]
    return out


PAGE_MAP_SQL = """
create table if not exists public.page_map (
  library_id  text not null references public.documents (library_id),
  page        int  not null,
  summary     text not null,
  words       tsvector generated always as (public.page_words(summary)) stored,
  embedding   extensions.vector(384),
  model       text,
  written     timestamptz not null default now(),
  primary key (library_id, page)
);
create index if not exists page_map_words on public.page_map using gin (words);
"""


def ensure_table(cur):
    """Creates public.page_map when missing: only its own statements, never the whole schema.sql, whose
    "alter table public.pages add column if not exists" takes an exclusive lock on the pages table and waited
    behind the running readers (9 Nov 2026, the first Document map run hung on it and was cancelled)."""
    cur.execute("select to_regclass('public.page_map')")
    if cur.fetchone()[0] is None:
        cur.execute(PAGE_MAP_SQL)
        cur.connection.commit()


def documents_to_map(cur, doc_types, companies, year_min, max_docs):
    """[(library_id, pages not yet mapped)] of the key documents, newest first, companies given or all."""
    q = ("select d.library_id, d.year, count(p.page) from public.documents d join public.pages p using (library_id) "
         "left join public.page_map m on m.library_id = p.library_id and m.page = p.page "
         "where d.doc_type = any(%s) and coalesce(d.year, 0) >= %s and m.page is null"
         + (" and d.wba_id = any(%s)" if companies else "") +
         " group by d.library_id, d.year order by d.year desc, d.library_id limit %s")
    params = [doc_types, year_min] + ([companies] if companies else []) + [max_docs]
    cur.execute(q, params)
    return [(lid, n) for lid, _, n in cur.fetchall()]


def unmapped_pages(cur, lid):
    cur.execute("select p.page, p.body from public.pages p left join public.page_map m using (library_id, page) "
                "where p.library_id = %s and m.page is null order by p.page", (lid,))
    return cur.fetchall()


def summarise(pages, usage=None, call=None, sess=None):
    """({page: line}, usage of this call) for up to PAGES_PER_CALL pages, one model call. usage: a Counter to add
    to; a fresh one when None (threads each get their own and the caller merges)."""
    if call is None:
        from model_reader import call
    u = collections.Counter() if usage is None else usage
    reply = call([{"role": "user", "content": page_prompt(pages)}], u, sess=sess, system=SYSTEM, max_tokens=60 * len(pages) + 100)
    return parse_lines(reply, pages), u


def write_lines(cur, lid, lines, model, commit):
    if not lines or not commit:
        return 0
    cur.executemany("insert into public.page_map (library_id, page, summary, model) values (%s, %s, %s, %s) "
                    "on conflict (library_id, page) do nothing", [(lid, p, s, model) for p, s in lines.items()])
    return len(lines)


def gold_companies(token):
    """The WBA ids of the companies with validated answers in the gold bases (narrative_eval.read_base)."""
    from narrative_eval import read_base, gold_id
    import json as J
    cfg = J.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "gold_attributes.json"), encoding="utf-8"))
    out = set()
    for b in cfg["bases"].values():
        for r in read_base(token, b, "Metadata", ["Metadata ID"]):
            wba, _, _ = gold_id(r["fields"].get("Metadata ID"))
            if wba:
                out.add(wba)
    return sorted(out)


def embedder():
    try:
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer(EMBED_MODEL)
    except Exception as e:                                  # library or model not installed: lexical map only
        print(f">>> embeddings unavailable ({type(e).__name__}: {str(e)[:80]})", flush=True)
        return None


def embed_missing(cur, model, commit, batch=256, limit=200000):
    """Fill embedding for lines without one. Returns rows embedded."""
    done = 0
    while done < limit:
        cur.execute("select library_id, page, summary from public.page_map where embedding is null limit %s", (batch,))
        rows = cur.fetchall()
        if not rows:
            break
        vecs = model.encode([r[2] for r in rows], normalize_embeddings=True, batch_size=64)
        if commit:
            cur.executemany("update public.page_map set embedding = %s::vector where library_id = %s and page = %s",
                            [("[" + ",".join(f"{x:.6f}" for x in v) + "]", r[0], r[1]) for r, v in zip(rows, vecs)])
            cur.connection.commit()
        done += len(rows)
        if not commit:
            break
    return done


def map_query(attr):
    """The text an attribute is searched by on the map: its name, the first 300 characters of its definition and
    a few labels when it has them. Works with a built spec (name and definition only)."""
    parts = [attr.get("name", ""), (attr.get("definition") or "")[:300]] + list(attr.get("labels", [])[:6])
    return " ".join(p for p in parts if p)


def map_pages(cur, ids, query, k, qvec=None):
    """[(library_id, page, body, score)] of the k pages of these documents whose map lines best match the
    query: word rank (websearch, 'simple') scaled to its best, plus cosine of the embeddings when both sides
    have one, half and half. Documents without a map return nothing (the caller keeps today's pages then)."""
    from extract_eval import words_query
    q = words_query([query])
    vec = ("[" + ",".join(f"{x:.6f}" for x in qvec) + "]") if qvec is not None else None
    cur.execute("select m.library_id, m.page, ts_rank_cd(m.words, to_tsquery('simple', %s)) as rk, "
                "case when m.embedding is null or %s is null then null else 1 - (m.embedding <=> %s::vector) end as cos "
                "from public.page_map m where m.library_id = any(%s)", (q, vec, vec, ids))
    rows = cur.fetchall()
    if not rows:
        return []
    best = max((r[2] or 0) for r in rows) or 1.0
    scored = []
    for lid, page, rk, cos in rows:
        s = (rk or 0) / best
        scored.append((0.5 * s + 0.5 * cos if cos is not None else s, lid, page))
    scored.sort(reverse=True)
    top = scored[:k]
    cur.execute("select library_id, page, body from public.pages where library_id = any(%s) and page = any(%s)",
                (sorted({l for _, l, _ in top}), sorted({p for _, _, p in top})))
    bodies = {(l, p): b for l, p, b in cur.fetchall()}
    return [(lid, page, bodies.get((lid, page), ""), s) for s, lid, page in top if (lid, page) in bodies]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["build", "embed", "status"])
    ap.add_argument("--doc-types", default=",".join(KEY_TYPES))
    ap.add_argument("--companies", default="gold", help="gold (companies with validated answers), all, or WBA ids comma-separated")
    ap.add_argument("--years", type=int, default=YEAR_MIN, help="documents of this year or later")
    ap.add_argument("--max-docs", type=int, default=200)
    ap.add_argument("--max-usd", type=float, default=5.0)
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    import evidence_db
    from model_reader import cost, DEFAULT_MODEL
    conn = evidence_db.connect()
    cur = conn.cursor()
    cur.execute("set statement_timeout = '120s'")
    ensure_table(cur)
    types = [t.strip() for t in a.doc_types.split(",") if t.strip()]
    if a.command == "status":
        cur.execute("select count(*), count(embedding) from public.page_map")
        n, e = cur.fetchone()
        cur.execute("select count(*) from public.pages p join public.documents d using (library_id) where d.doc_type = any(%s) and coalesce(d.year, 0) >= %s", (types, a.years))
        total = cur.fetchone()[0]
        print(f"page_map: {n:,} lines ({e:,} with an embedding) of {total:,} pages in the key documents since {a.years}")
        return 0
    if a.command == "embed":
        m = embedder()
        if not m:
            return 1
        print(f"embedded {embed_missing(cur, m, a.commit):,} lines" + ("" if a.commit else " (dry run: one batch, nothing written)"))
        return 0
    companies = None
    if a.companies == "gold":
        companies = gold_companies(os.environ["GOLD_TOKEN"].strip())
        print(f">>> {len(companies)} companies with validated answers", flush=True)
    elif a.companies != "all":
        companies = [c.strip() for c in a.companies.split(",") if c.strip()]
    docs = documents_to_map(cur, types, companies, a.years, a.max_docs)
    print(f">>> {len(docs)} documents with {sum(n for _, n in docs):,} pages to map" + ("" if a.commit else " (dry run: first document only, nothing written)"), flush=True)
    usage = collections.Counter()
    model = os.environ.get("READER_MODEL") or DEFAULT_MODEL
    written = 0
    t0 = time.time()
    for i, (lid, _) in enumerate(docs if a.commit else docs[:1]):
        pages = unmapped_pages(cur, lid)
        batches = [pages[j:j + PAGES_PER_CALL] for j in range(0, len(pages), PAGES_PER_CALL)]
        lines = {}
        with concurrent.futures.ThreadPoolExecutor(THREADS) as ex:
            for got, u in ex.map(lambda b: summarise(b), batches):
                lines.update(got); usage.update(u)
        written += write_lines(cur, lid, lines, model, a.commit)
        if a.commit:
            conn.commit()
        else:
            for p, s in sorted(lines.items())[:15]:
                print(f"  {lid} p{p}: {s}")
        if (i + 1) % 10 == 0 or not a.commit:
            print(f"  {i + 1} documents, {written:,} lines, {usage['calls']} calls, ${cost(usage):.2f}, {time.time() - t0:.0f}s", flush=True)
        if cost(usage) >= a.max_usd:
            print(f">>> budget of ${a.max_usd} reached", flush=True)
            break
    print(f"Model calls: {usage['calls']} ({usage['model_errors']} failed); input tokens {usage['input_tokens']:,}; output tokens "
          f"{usage['output_tokens']:,}; cost about ${cost(usage):.2f}. Lines written: {written:,}.", flush=True)
    if a.commit:
        m = embedder()
        if m:
            print(f"embedded {embed_missing(cur, m, True):,} lines", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
