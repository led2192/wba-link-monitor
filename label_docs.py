#!/usr/bin/env python3
"""
label_docs.py: a type, a language and a title for the documents that have text and no type.

Why (19 Nov 2026). Of 285,848 documents with text, 55,593 had no language and 54,392 of those no type: the Airtable
AI field never reached them (it reads a cover text they do not have), so they count for no coverage number and no
filter by type finds them. A report a person sends enters the same way: "Document 2026", no type, and an answer
that asks for the later reports of a company does not see it. The text of these documents is in the evidence
database, so the first pages are read from there.

What it does. Documents with text, no type and not yet looked at (table doc_labels), the newest found first. A model
is shown the first pages (the question of doc_label_trial.py, measured the same day on 36 documents) and answers the
type from the library's list, the language and the title as printed. Two models:

    --model local    an open model on the job server (Qwen3 8B, about 40 seconds a document, no cost): the documents
                     that arrive each day, started every hour by the server's clock
    --model paid     the reader's paid model (about a second and 0.0003 USD a document), for a backlog; stops at
                     --max-usd

What is written, and only where the row is empty (the row is read first; nothing a person or the AI field wrote is
touched; no link field):

    source_type_check   when it and source_type are both empty (UNKNOWN is written too, so the row leaves the queue)
    doc_language        when empty and the language is one the field already knows
    auto_title          when the AI field gave no title and auto_title is empty, "Untitled", "Document ...", or
                        longer than 90 characters

A page whose text is not readable (under four letters in ten: a scan read badly, a font with no mapping) gets no
title and no type: a model shown noise invents. Every document looked at gets a line in doc_labels, so it is not
looked at twice.

    python label_docs.py [--model local|paid] [--limit 60] [--from-year 2023] [--max-usd 5] [--threads 8] [--commit]

Dry run prints counts only. The log is public: never a title, an address or a company. Env: SUPABASE_DB_URL,
AIRTABLE_TOKEN, AIRTABLE_BASE; ANTHROPIC_API_KEY for --model paid; HF_HOME for --model local.
"""
import argparse, collections, json, os, re, sys, time, unicodedata

import doc_label_trial as T

API = "https://api.airtable.com/v0"
LOCAL_MODEL = "qwen3-8b"
LANGUAGES = {"English", "Portuguese", "French", "Chinese (Simplified)", "Chinese (Traditional)", "German", "Japanese", "Spanish", "Arabic", "Russian",
             "Thai", "Korean", "Vietnamese", "Greek", "Norwegian", "Indonesian", "Turkish", "Hindi", "Italian", "Polish", "Romanian", "Dutch",
             "Swedish", "Danish", "Finnish", "Czech", "Hungarian", "Hebrew", "Malay", "Ukrainian", "Croatian", "Catalan"}
QUEUE_SQL = """
select d.library_id, d.record_id,
       (select string_agg(p.body, E'\\n' order by p.page) from public.pages p where p.library_id = d.library_id and p.page <= 3)
from public.documents d
where d.text_status = 'ok' and coalesce(d.pages, 0) >= 1 and coalesce(d.doc_type, '') = '' and d.record_id is not null
  and not exists (select 1 from public.doc_labels l where l.library_id = d.library_id)
  and (%s::int is null or d.year >= %s::int)
order by d.found desc nulls last, d.library_id limit %s"""


FAMILY = {"LATIN": "latin", "FULLWIDTH": "latin", "HALFWIDTH": "east", "CJK": "east", "HIRAGANA": "east", "KATAKANA": "east", "HANGUL": "east", "BOPOMOFO": "east", "IDEOGRAPHIC": "east"}


def scripts(text):
    """How many families of writing the letters belong to (Latin; East Asian; each other script its own). Japanese
    is two. Text decoded with the wrong table is letters of five or six unrelated scripts at once."""
    seen = set()
    for ch in text:
        if unicodedata.category(ch)[0] == "L":
            word = unicodedata.name(ch, "X").split(" ")[0]
            seen.add(FAMILY.get(word, word))
    return len(seen)


def readable(text):
    """Is this the text of a page, or noise? Letters of any script among the characters that are not spaces, few
    characters that are no character at all, and not a mix of unrelated scripts."""
    chars = [ch for ch in (text or "")[:T.CHARS] if not ch.isspace()]
    if len(chars) < 40:
        return False
    letters = sum(1 for ch in chars if unicodedata.category(ch)[0] == "L")
    odd = sum(1 for ch in chars if unicodedata.category(ch) in ("Co", "Cn", "Cc") or ch == "\ufffd")
    return letters >= 0.4 * len(chars) and odd <= 0.05 * len(chars) and scripts("".join(chars)) <= 3


def clean_title(title):
    """The title as it may be written: one line, 120 characters at most, '' when it is not words."""
    t = " ".join(str(title or "").split())[:120]
    letters = sum(1 for ch in t if unicodedata.category(ch)[0] == "L")
    odd = sum(1 for ch in t if unicodedata.category(ch) in ("Co", "Cn", "Cc") or ch == "\ufffd")
    return t if len(t) >= 4 and letters >= 0.5 * len(t.replace(" ", "")) and not odd and scripts(t) <= 2 else ""


def language_name(name):
    n = " ".join(str(name or "").split()).title().replace("(Simplified)", "(Simplified)")
    if n in ("Chinese", "Mandarin", "Simplified Chinese"):
        n = "Chinese (Simplified)"
    if n == "Traditional Chinese":
        n = "Chinese (Traditional)"
    return n if n in LANGUAGES else ""


def poor_title(t):
    t = (t or "").strip()
    return not t or t == "Untitled" or len(t) > 90 or bool(re.match(r"^(document|unknown)\b", t, re.I))


def row_updates(fields, typ, language, title):
    """What to write on a row, given what it holds. Only empty places; no link field."""
    val = lambda k: (fields.get(k).get("name") if isinstance(fields.get(k), dict) else fields.get(k)) or ""
    ai_title = fields.get("source_title"); ai_title = (ai_title.get("value") if isinstance(ai_title, dict) else ai_title) or ""
    upd = {}
    if typ and not val("source_type_check") and not val("source_type"):
        upd["source_type_check"] = typ
    lang = language_name(language)
    if lang and not val("doc_language"):
        upd["doc_language"] = lang
    t = clean_title(title)
    if t and not str(ai_title).strip() and not val("curated_doc_title") and poor_title(val("auto_title")):
        upd["auto_title"] = t
    return upd


def airtable(method, url, token, payload=None, session=None, tries=4, sleep=time.sleep):
    """One call with the retries every Airtable call needs (rate limit, a passing error). Returns the JSON."""
    import requests
    s = session or requests
    for i in range(tries):
        try:
            r = s.request(method, url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                          data=json.dumps(payload) if payload is not None else None, timeout=60)
            if r.status_code in (429, 500, 502, 503, 504):
                sleep(2 * (i + 1)); continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == tries - 1:
                raise
            sleep(2 * (i + 1))
    raise RuntimeError("Airtable did not answer")


def asker(model, threads):
    """(name, ask(text) -> reply, spent() -> USD)."""
    if model == "paid":
        import model_reader
        usage = collections.Counter()
        name = os.environ.get("READER_MODEL") or model_reader.DEFAULT_MODEL

        def ask(text):
            for _ in range(2):                                      # a reply that is not the JSON asked for is asked for once more
                reply = model_reader.call([{"role": "user", "content": text}], usage, max_tokens=400)
                if T.parse_reply(reply or "")[0]:
                    return reply
            return reply
        return name, ask, lambda: model_reader.cost(usage)
    from huggingface_hub import hf_hub_download
    from llama_cpp import Llama
    repo, file, suffix = T.MODELS[LOCAL_MODEL]
    llm = Llama(model_path=hf_hub_download(repo, file), n_ctx=8192, n_threads=threads, verbose=False)
    sch = T.schema()
    ask = lambda text: llm.create_chat_completion(messages=[{"role": "user", "content": text + suffix}], response_format={"type": "json_object", "schema": sch},
                                                  temperature=0, max_tokens=160)["choices"][0]["message"]["content"] or ""
    return LOCAL_MODEL, ask, lambda: 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=["local", "paid"], default="local")
    ap.add_argument("--limit", type=int, default=60, help="documents this run")
    ap.add_argument("--from-year", type=int, default=None, help="only documents of this year or later")
    ap.add_argument("--max-usd", type=float, default=5.0, help="the paid model stops at this cost")
    ap.add_argument("--minutes", type=int, default=50, help="stop cleanly after this many minutes")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    for v in ("SUPABASE_DB_URL",) + (("AIRTABLE_TOKEN", "AIRTABLE_BASE") if a.commit else ()):
        if not os.environ.get(v):
            sys.exit(f"Set {v}.")
    import qa, evidence_db
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("label_docs.py")
    c = collections.Counter(); t0 = time.time()
    with evidence_db.connect() as conn:
        cur = conn.cursor()
        cur.execute("select to_regclass('public.doc_labels') is not null")
        if not cur.fetchone()[0]:
            print("public.doc_labels is not there yet: nothing to do.")
            run.finish("ok", rows_in=0, rows_out=0, notes="no table"); return 0
        cur.execute(QUEUE_SQL, (a.from_year, a.from_year, a.limit))
        docs = cur.fetchall()
        print(f">>> {len(docs)} documents with text and no type to look at ({a.model}, limit {a.limit}" + (f", from {a.from_year}" if a.from_year else "") + ")", flush=True)
        if not docs:
            run.finish("ok", rows_in=0, rows_out=0, notes="nothing waiting"); return 0
        name, ask, spent = asker(a.model, a.threads)
        base = f"{API}/{os.environ.get('AIRTABLE_BASE', '')}/report_library"
        token = os.environ.get("AIRTABLE_TOKEN", "")
        for lid, rec, cover in docs:
            if spent() >= a.max_usd:
                c["stopped at the cost cap"] = 1; break
            if time.time() - t0 > a.minutes * 60:
                c["stopped at the time cap"] = 1; break
            typ = language = title = None
            if not readable(cover):
                c["not readable"] += 1
            else:
                try:
                    typ, language, title = T.parse_reply(ask(T.told(cover)) or "")
                except Exception as e:
                    if type(e).__name__ == "ModelRefused":
                        c["stopped: the model refused a call"] = 1; break
                    typ = None
                c["answered" if typ else "no usable reply"] += 1
            written = ""
            if a.commit:
                if typ:
                    try:
                        fields = airtable("GET", f"{base}/{rec}", token).get("fields", {})
                        upd = {} if fields.get("discard") else row_updates(fields, typ, language, title)
                        if upd:
                            airtable("PATCH", f"{base}/{rec}", token, {"fields": upd, "typecast": True})     # no link field among them
                            written = ",".join(sorted(upd)); c["rows written"] += 1
                            for k in upd:
                                c[k] += 1
                        time.sleep(0.22)
                    except Exception as e:                           # the row is left for another run: no line in doc_labels
                        c["write failed"] += 1
                        run.failure("DATA_CHECK", f"a document's type and title were read and not written ({type(e).__name__})", key=f"label_docs|{lid}")
                        continue
                cur.execute("insert into public.doc_labels (library_id, model, type, language, title, written) values (%s, %s, %s, %s, %s, %s) on conflict (library_id) do nothing",
                            (lid, name, typ, language_name(language) or None, clean_title(title) or None, written or None))
                conn.commit()
            c["looked at"] += 1
            if c["looked at"] % 20 == 0:
                print(f"  {c['looked at']} looked at, {c['rows written']} rows written, {spent():.4f} USD, {(time.time() - t0) / 60:.0f} min", flush=True)
    print(f">>> {dict(c)}; {spent():.4f} USD; {(time.time() - t0) / 60:.1f} min" + ("" if a.commit else " (dry run: nothing written)"), flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(f"### Type, language and title for documents with none\n\n{name}: {json.dumps(dict(c))}; {spent():.4f} USD" + ("" if a.commit else " (dry run)") + "\n")
    run.finish("ok", rows_in=len(docs), rows_out=c["rows written"], counters={**dict(c), "usd": round(spent(), 4)}, notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
