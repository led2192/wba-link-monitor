#!/usr/bin/env python3
"""
semantic_index.py: the semantic index of the main reports, by passage.

Lantern (the one-box search) and the robot's page ranking find pages by words. Words miss meaning and
language: "headcount" does not find "number of employees", "plantilla" finds nothing in an English report.
The owner's decision (10 Nov 2026): a semantic index limited to the main reports (annual, integrated,
sustainability, ESG and CSR reports) from 2024 on, by passage rather than by page (the document map's one
line per page could not say which page prints a figure), merged with the word ranking only once measured
against the validated answers. Sized on 10 Nov 2026: 14,877 documents, 1.22 million pages, about 5.3 million
passages of 120 words.

What it does: for every document in scope with no chunks yet, reads its pages from the evidence database,
cuts each page into passages of about 120 words by sentences (evidence_chunks.passages, never across pages),
embeds them with an open multilingual sentence model (MODEL, 384 dimensions, run on the GitHub runner's CPU:
no API, no cost) and writes them to public.chunks with their embedding; public.chunk_docs marks the document
done, so a run stopped by its time cap loses nothing and the next run continues. The e5 models want the text
prefixed "passage: " when indexed and "query: " when searched; both are here (embed_passages, embed_query).

In the same pass (the pages are read once) it writes each page's neighbouring-word pairs to public.page_pairs
("scope 1" -> scope_1, "act core" -> act_core), with a word index on them. lanterN's word index knows which
pages hold two words, not whether they stand together: a phrase had to be checked page by page, a millisecond
each from disk, so a rare phrase of two common words ("ACT Core") was searched in a sample and could find
nothing (10 Nov 2026). With the pairs, a phrase is one index question. Same scope as the passages.

    python semantic_index.py [--commit] [--limit N] [--minutes M] [--partition k/N] [--years 2024-] [--batch 64]

Dry run by default: reads --limit documents (20), cuts and embeds them, prints counts, throughput and
examples, writes nothing. --partition k/N takes the documents whose hash falls in slice k, so N runs can
share the work (each with its own database connection; the database allows 15).
Env: SUPABASE_DB_URL; AIRTABLE_QA_TOKEN for the run record (QA_COMMIT=true writes it).
"""
import argparse, collections, os, sys, time

MODEL = "intfloat/multilingual-e5-small"     # 384 dimensions, 100+ languages, 118M parameters: about 100 passages/s on 4 CPU cores
DIMS = 384
MAIN_TYPES = ("Annual Report", "Integrated Report", "Sustainability Report", "ESG Report", "CSR Report")
YEAR_MIN = 2024
MAX_SEQ = 256                                # tokens read per passage (120 words are about 180 tokens)
INSERT_BATCH = 500


def parse_partition(text):
    """'k/N' -> (k, N); None or '' -> (0, 1)."""
    if not text:
        return 0, 1
    k, n = text.split("/")
    k, n = int(k), int(n)
    if not (n >= 1 and 0 <= k < n):
        raise ValueError(f"partition {text!r}: k must be 0..N-1")
    return k, n


def parse_years(text):
    """'2024-' -> (2024, None); '2023-2024' -> (2023, 2024); '2025' -> (2025, 2025)."""
    text = (text or f"{YEAR_MIN}-").strip()
    if text.endswith("-"):
        return int(text[:-1]), None
    if "-" in text:
        a, b = text.split("-")
        return int(a), int(b)
    return int(text), int(text)


def documents_to_index(cur, years, partition, limit):
    """[(library_id, year, doc_type, pages)] in scope and not yet done, newest year first, in slice k of N."""
    y0, y1 = years
    k, n = partition
    cur.execute("select to_regclass('public.chunk_docs') is not null")
    has_done = cur.fetchone()[0]                               # a dry run before the tables exist still lists the documents
    cur.execute(f"""
        select d.library_id, d.year, d.doc_type, coalesce(d.pages, 0)
        from public.documents d
        where d.doc_type = any(%s) and d.text_status = 'ok' and d.year >= %s and (%s::int is null or d.year <= %s)
          {"and not exists (select 1 from public.chunk_docs c where c.library_id = d.library_id)" if has_done else ""}
          and (abs(hashtext(d.library_id)) %% %s) = %s
        order by d.year desc, d.library_id
        limit %s""", (list(MAIN_TYPES), y0, y1, y1, n, k, limit))
    return cur.fetchall()


def chunk_pages(pages):
    """[(page, n, body)] for one document's [(page, text)]: passages of about 120 words, by sentences, never across pages."""
    from evidence_chunks import passages
    out = []
    for page, text in pages:
        for i, t in enumerate(passages(text)):
            if len(t.split()) >= 8:                  # a stub (a page number, a header alone) says nothing
                out.append((page, i, t))
    return out


PAIR_WORD = __import__("re").compile(r"[^\W_]+", __import__("re").UNICODE)
MAX_PAIR_WORD = 40                           # a longer run of letters is a broken extraction, not a word


def pair_lexemes(text):
    """The neighbouring-word pairs of a page, as lexemes 'word_word' in lower case, sorted and unique.
    Words are runs of letters and digits, as the 'simple' text configuration cuts them ("Scope 1" -> scope_1)."""
    words = PAIR_WORD.findall((text or "").lower())
    return sorted({f"{a}_{b}" for a, b in zip(words, words[1:]) if len(a) <= MAX_PAIR_WORD and len(b) <= MAX_PAIR_WORD})


def pairs_literal(lexemes):
    """A tsvector literal from lexemes (letters, digits and '_' only, so quoting is enough)."""
    return " ".join("'" + x.replace("'", "") + "'" for x in lexemes)


def pair_rows(pages):
    """[(page, tsvector literal)] for one document's [(page, text)]; pages without two words are left out."""
    out = []
    for page, text in pages:
        lex = pair_lexemes(text)
        if lex:
            out.append((page, pairs_literal(lex)))
    return out


def vector_literal(v):
    """A pgvector literal from a sequence of floats."""
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


class Embedder:
    """The sentence model, loaded once. embed_passages / embed_query add the e5 prefixes; vectors are unit length."""

    def __init__(self, model=MODEL, batch=64):
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(model)
        self.model.max_seq_length = MAX_SEQ
        self.batch = batch
        self.name = model

    def embed_passages(self, texts):
        return self.model.encode(["passage: " + t for t in texts], batch_size=self.batch, normalize_embeddings=True, show_progress_bar=False)

    def embed_query(self, text):
        return self.model.encode(["query: " + text], normalize_embeddings=True, show_progress_bar=False)[0]


def write_document(cur, lid, chunks, vectors, model, pairs=None):
    """The document's chunks with their embeddings, its pages' word pairs (pairs: [(page, tsvector literal)], when the
    page_pairs table exists), then its done-marker. One transaction per document (the caller commits)."""
    for i in range(0, len(pairs or []), INSERT_BATCH):
        cur.executemany("insert into public.page_pairs (library_id, page, pairs) values (%s, %s, %s::tsvector) "
                        "on conflict (library_id, page) do update set pairs = excluded.pairs",
                        [(lid, page, lit) for page, lit in pairs[i:i + INSERT_BATCH]])
    rows = [(lid, page, n, body, vector_literal(v), model) for (page, n, body), v in zip(chunks, vectors)]
    for i in range(0, len(rows), INSERT_BATCH):
        cur.executemany("insert into public.chunks (library_id, page, n, body, embedding, model) values (%s, %s, %s, %s, %s::extensions.vector, %s) "
                        "on conflict (library_id, page, n) do update set body = excluded.body, embedding = excluded.embedding, model = excluded.model",
                        rows[i:i + INSERT_BATCH])
    cur.execute("insert into public.chunk_docs (library_id, chunks, model) values (%s, %s, %s) on conflict (library_id) do update set chunks = excluded.chunks, model = excluded.model, written = now()",
                (lid, len(rows), model))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=20, help="documents this run (dry run: 20)")
    ap.add_argument("--minutes", type=float, default=330, help="stop cleanly after this many minutes (the job is killed at 360)")
    ap.add_argument("--partition", default="", help="k/N: this run takes slice k of N of the documents (parallel runs)")
    ap.add_argument("--years", default=f"{YEAR_MIN}-", help="reporting years in scope, e.g. 2024- or 2023-2024")
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--model", default=MODEL)
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import qa, evidence_db
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("semantic_index.py")
    t0 = time.time()
    years, partition = parse_years(a.years), parse_partition(a.partition)
    emb = Embedder(a.model, a.batch)
    done, n_chunks, n_pages, n_pairs, examples, stopped = 0, 0, 0, 0, [], None
    embed_s, lost = 0.0, 0
    with evidence_db.connect() as conn:
        cur = conn.cursor()
        cur.execute("set statement_timeout = '120s'")
        cur.execute("select to_regclass('public.page_pairs') is not null")
        has_pairs = cur.fetchone()[0]                          # the table arrives with a full Evidence database load
        docs = documents_to_index(cur, years, partition, a.limit if a.limit else 1_000_000)
        print(f">>> {len(docs):,} documents to index (years {a.years}, slice {partition[0]}/{partition[1]}), model {a.model}", flush=True)
        for lid, year, dtype, pages_n in docs:
            if time.time() - t0 > a.minutes * 60:
                stopped = f"stopped at the time cap of {a.minutes:g} minutes"
                break
            try:
                cur.execute("select page, body from public.pages where library_id = %s order by page", (lid,))
                pages = cur.fetchall()
                chunks = chunk_pages(pages)
                if not chunks:
                    if a.commit:
                        cur.execute("insert into public.chunk_docs (library_id, chunks, model) values (%s, 0, %s) on conflict (library_id) do nothing", (lid, a.model))
                        conn.commit()
                    continue
                t1 = time.time()
                vectors = emb.embed_passages([c[2] for c in chunks])
                embed_s += time.time() - t1
                prs = pair_rows(pages)
                if a.commit:
                    write_document(cur, lid, chunks, vectors, a.model, prs if has_pairs else None)
                    conn.commit()
            except Exception as e:
                # The database dropped the connection (11 Nov 2026: two runs died an hour in, each with hours of work
                # ahead). A new connection, and the next document: this one is not marked done, the next run takes it.
                # Also a connection the driver itself gave up on (11 Nov 2026, 13:39 UTC: "lost synchronization with
                # server", a DatabaseError, killed partition 6/8 six minutes in): closed or broken is lost, whatever
                # the error is called.
                gone = type(e).__name__ == "OperationalError" or bool(getattr(conn, "closed", False)) or bool(getattr(conn, "broken", False))
                if not gone or lost >= 5:
                    raise
                lost += 1
                print(f"  connection lost ({str(e)[:80]}); reconnecting ({lost} of 5)", flush=True)
                time.sleep(20 * lost)
                conn = evidence_db.connect(); cur = conn.cursor(); cur.execute("set statement_timeout = '120s'")
                continue
            done += 1; n_chunks += len(chunks); n_pages += len(pages); n_pairs += sum(p.count(" ") + 1 for _, p in prs)
            if len(examples) < 5:
                examples.append((lid, year, dtype, len(pages), len(chunks), chunks[min(2, len(chunks) - 1)][2][:140]))
            if done % 50 == 0:
                print(f"  {done:,} documents, {n_chunks:,} chunks, {n_chunks / max(embed_s, 1e-9):.0f} chunks/s embedding, {(time.time() - t0) / 60:.0f} min", flush=True)
    secs = time.time() - t0
    md = [f"### Semantic index ({'written' if a.commit else 'dry run'})", "",
          f"{done:,} documents, {n_pages:,} pages, {n_chunks:,} chunks in {secs / 60:.1f} min ({n_chunks / max(embed_s, 1e-9):.0f} chunks/s embedding, "
          f"{n_chunks / max(secs, 1e-9):.0f} chunks/s in all); model {a.model}, {DIMS} dimensions; years {a.years}, slice {partition[0]}/{partition[1]}."
          + f" Word pairs: {n_pairs:,} ({n_pairs / max(n_pages, 1):.0f} a page){'' if has_pairs else '; the page_pairs table is not there yet, none written'}."
          + (f" {stopped}." if stopped else ""), ""]
    if examples:
        md += ["Examples (library_id, year, type, pages, chunks, a chunk):", ""] + [f"- {l} | {y} | {t} | {p} | {c} | {x}" for l, y, t, p, c, x in examples]
    out = "\n".join(md) + "\n"
    open("semantic_index.md", "w", encoding="utf-8").write(out)
    print(out, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(out + "\n")
    run.finish("ok", rows_in=done, rows_out=n_chunks if a.commit else 0,
               counters={"documents": done, "pages": n_pages, "chunks": n_chunks, "pairs": n_pairs, "chunks_per_s": round(n_chunks / max(embed_s, 1e-9)),
                         "minutes": round(secs / 60, 1), "slice": a.partition or "0/1", "years": a.years},
               notes=stopped or ("written" if a.commit else "dry run"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
