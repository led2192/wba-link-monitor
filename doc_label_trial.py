#!/usr/bin/env python3
"""
doc_label_trial.py: can an open model on the job server, at no cost, give a document its type, language and title?

Why (19 Nov 2026). Of the 285,848 documents with text, 55,593 have no language and 54,392 of those no type: the
Airtable AI field never reached them, so they do not count for completeness and no filter by type finds them. And
37,000 documents carry a "title" that is the first words of their cover, sometimes 200 characters of it. The paid
reader could do all of this, at a cost a document; the server's processors are idle most of the day.

What it does. It takes the same number of documents from three groups, the same ones on every run (ordered by a
hash of their id):

    english   language English, with a type           the stored type is the reference (the AI field: 93% right)
    other     another language, with a type            the stored type is the reference
    bare      no language and no type                  nothing to compare with: what share gets a type at all

Each model is shown the first pages (2,000 characters, the prompt of type_covers.py) and must answer, in JSON it
cannot leave (a grammar), the type from the list, the language, and the title as printed. Printed: for each model
and group, how many answered, how many gave the stored type, how many gave any type, seconds a document. With
--commit each answer goes to public.doc_label_trials, where the titles can be read beside the stored ones.

It is a trial: nothing it writes is read by the search, the library or the coverage numbers. The log is public: it
prints counts, shares and seconds only, never a title, an address or a company.

    python doc_label_trial.py [--models qwen3-4b,qwen3-8b] [--each 15] [--threads 8] [--commit]
    python doc_label_trial.py --models paid --max-usd 0.20 [--each 12] [--commit]

"paid" is the reader's own paid model (model_reader.call), shown the same documents and the same question, to set
beside the open models before either is given the backlog. It stops at --max-usd and prints what it spent.

Env: SUPABASE_DB_URL; HF_HOME. Needs llama-cpp-python and huggingface_hub (/srv/lantern/venv-llm on the server).
"""
import argparse, json, os, statistics, sys, time

import type_covers
from fact_classify_trial import MODELS

GROUPS = ("english", "other", "bare")
CHARS = 2000
WHERE = {"english": "d.lang = 'English' and coalesce(d.doc_type, '') <> ''",
         "other": "d.lang is not null and d.lang <> 'English' and coalesce(d.doc_type, '') <> ''",
         "bare": "d.lang is null and coalesce(d.doc_type, '') = ''"}
SAMPLE_SQL = """
select d.library_id, d.doc_type, d.lang,
       (select string_agg(p.body, E'\\n' order by p.page) from public.pages p where p.library_id = d.library_id and p.page <= 3)
from public.documents d
where d.text_status = 'ok' and d.pages >= 1 and {where}
order by md5(d.library_id) limit %s"""
TABLE_SQL = """
create table if not exists public.doc_label_trials (
  id bigint generated always as identity primary key, at timestamptz not null default now(), model text not null,
  library_id text not null, grp text not null, type text, language text, title text, seconds real, run text);
alter table public.doc_label_trials enable row level security;"""


def choices():
    return list(type_covers.TYPES) + [type_covers.UNKNOWN]


def told(cover):
    """What the model reads: the paid reader's own question, and the language asked for besides."""
    base = type_covers.prompt((cover or "")[:CHARS], version=1)
    cut = base.rindex("Reply with JSON")
    return (base[:cut] + 'Reply with JSON: {"type": "<one type exactly as listed>", "language": "<the language the document is written in, '
            'in English: English, Japanese, Spanish ...>", "title": "<the document\'s title as printed on its cover, in its language, without '
            'slogans or the lines under it, up to 120 characters; empty when the pages show no title>"}')


def schema():
    return {"type": "object", "required": ["type", "language", "title"], "additionalProperties": False,
            "properties": {"type": {"type": "string", "enum": choices()}, "language": {"type": "string", "maxLength": 30},
                           "title": {"type": "string", "maxLength": 140}}}


def parse_reply(text):
    """(type, language, title), or (None, None, None) when the reply is not the JSON asked for."""
    try:
        j = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except Exception:
        return None, None, None
    t = str(j.get("type") or "").strip()
    if t not in choices():
        return None, None, None
    return t, str(j.get("language") or "").strip()[:30], " ".join(str(j.get("title") or "").split())[:120]


def summarise(rows):
    """rows: [(stored type or '', stored language or '', type or None, language, title, seconds)] of one model and group."""
    ok = [r for r in rows if r[2] is not None]
    ref = [r for r in ok if r[0]]
    lang = [r for r in ok if r[1]]
    return {"documents": len(rows), "answered": len(ok), "with_reference": len(ref), "same_type": sum(1 for r in ref if r[2] == r[0]),
            "typed": sum(1 for r in ok if r[2] != type_covers.UNKNOWN), "titled": sum(1 for r in ok if r[4]),
            "lang_reference": len(lang), "same_language": sum(1 for r in lang if (r[3] or "").lower() == r[1].lower()),
            "seconds": round(statistics.median([r[5] for r in rows]), 1) if rows else 0.0, "slowest": round(max([r[5] for r in rows], default=0.0), 1)}


def line(model, group, s):
    share = lambda a, b: f"{a} of {b} ({100 * a / b:.0f}%)" if b else "n/a"
    return (f"| {model} | {group} | {s['documents']} | {share(s['answered'], s['documents'])} | {share(s['same_type'], s['with_reference'])} | "
            f"{share(s['typed'], s['answered'])} | {share(s['same_language'], s['lang_reference'])} | {share(s['titled'], s['answered'])} | {s['seconds']} s, slowest {s['slowest']} s |")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="qwen3-4b,qwen3-8b")
    ap.add_argument("--each", type=int, default=15, help="documents of each of the three groups")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--commit", action="store_true", help="write each answer to public.doc_label_trials")
    ap.add_argument("--max-usd", type=float, default=0.20, help="the paid model stops at this cost")
    a = ap.parse_args()
    names = [m.strip() for m in a.models.split(",") if m.strip()]
    unknown = [m for m in names if m not in MODELS and m != "paid"]
    if unknown:
        sys.exit(f"unknown model(s): {', '.join(unknown)}; known: paid, {', '.join(MODELS)}")
    import psycopg
    run = os.environ.get("GITHUB_RUN_ID", "local")
    docs = []                                                       # (group, library_id, stored type, stored language, cover)
    with psycopg.connect(os.environ["SUPABASE_DB_URL"]) as conn:
        cur = conn.cursor()
        if a.commit:
            cur.execute(TABLE_SQL); conn.commit()
        for g in GROUPS:
            cur.execute(SAMPLE_SQL.format(where=WHERE[g]), (a.each,))
            docs += [(g, r[0], r[1] or "", r[2] or "", r[3] or "") for r in cur.fetchall()]
    print(f">>> {len(docs)} documents ({a.each} a group), {a.threads} threads", flush=True)
    md = ["### Type, language and title of documents by open models on the job server (a trial: nothing here is in use)", "",
          f"{a.each} documents a group, the same on every run. The stored type is the reference where there is one; it is itself right about 93% of the time on English documents.", "",
          "| model | group | documents | answered as asked | same type as stored | given a type | same language as stored | given a title | time a document (median) |", "|---|---|---|---|---|---|---|---|---|"]
    sch = schema()
    for name in names:
        if name == "paid":
            import collections
            import model_reader
            usage = collections.Counter()
            name = os.environ.get("READER_MODEL") or model_reader.DEFAULT_MODEL
            ask = lambda text: model_reader.call([{"role": "user", "content": text}], usage, max_tokens=400)
            spent = lambda: model_reader.cost(usage)
        else:
            from huggingface_hub import hf_hub_download
            from llama_cpp import Llama
            repo, file, suffix = MODELS[name]
            try:
                llm = Llama(model_path=hf_hub_download(repo, file), n_ctx=8192, n_threads=a.threads, verbose=False)
            except Exception as e:
                md.append(f"| {name} | not loaded: {type(e).__name__} | | | | | | | |"); print(f">>> {name}: not loaded ({type(e).__name__})", flush=True)
                continue
            ask = lambda text, llm=llm, suffix=suffix: llm.create_chat_completion(
                messages=[{"role": "user", "content": text + suffix}], response_format={"type": "json_object", "schema": sch}, temperature=0, max_tokens=160)["choices"][0]["message"]["content"] or ""
            spent = lambda: 0.0
        by, out = {g: [] for g in GROUPS}, []
        for n, (g, lid, stored, lang, cover) in enumerate(docs, 1):
            if spent() >= a.max_usd:
                print(f">>> {name}: stopped at {spent():.4f} USD after {n - 1} documents", flush=True); break
            t = time.time()
            try:
                typ, language, title = parse_reply(ask(told(cover)) or "")
            except Exception:
                typ, language, title = None, None, None
            secs = time.time() - t
            by[g].append((stored, lang, typ, language, title, secs)); out.append((name, lid, g, typ, language, title, secs, run))
            if n % 5 == 0:
                print(f"  {name}: {n} of {len(docs)}", flush=True)
        if spent():
            md.append(""); md.append(f"Paid model {name}: {usage['calls']} calls, {usage['input_tokens']:,} tokens in, {usage['output_tokens']:,} out, {spent():.4f} USD (stops at {a.max_usd}).")
            print(">>> " + md[-1], flush=True)
        for g in GROUPS:
            md.append(line(name, g, summarise(by[g]))); print(">>> " + md[-1], flush=True)
        if a.commit and out:
            with psycopg.connect(os.environ["SUPABASE_DB_URL"]) as conn:
                conn.cursor().executemany("insert into public.doc_label_trials (model, library_id, grp, type, language, title, seconds, run) values (%s, %s, %s, %s, %s, %s, %s, %s)", out)
                conn.commit()
            print(f">>> {name}: {len(out)} answers written to doc_label_trials", flush=True)
    text = "\n".join(md) + "\n"
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
