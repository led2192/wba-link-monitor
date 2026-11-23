#!/usr/bin/env python3
"""
company_profile.py: what a company is, read from its own reports.

Why (22 Nov 2026, the owner). lantrNa holds every document of a company and knew of the company only what a list from
outside said: an industry (one of 36), a country, a region. Someone who asks for "semiconductor companies" names a
sub-sector that list does not have. The reports themselves say where the company is based, where it operates, what it
owns and what it sells. This script reads that, company by company, into public.company_profile: one row a field,
with the page it was read from.

How. For each company, its newest long reports (30 pages or more, the last three years). Two questions are put to an
open model that runs on the job server (Qwen3 8B, no cost): who the company is (headquarters, country of
incorporation, year founded, listing, ownership, parent, main subsidiaries, countries of operation), and what it does
(business segments, products and services, customers, sector, industry, sub-sector, a description of 30 words). Each
question is shown the four pages of those reports that best hold its words (the words index, inside the company's own
documents only; nothing is logged as a search). The model answers in a fixed JSON shape, null where the pages do not
say, and names the excerpt each answer comes from.

What is written. One row a company and field (upsert): the value, the document and page, the model, the time. A field a
person confirmed or corrected (status other than 'read') is never overwritten. A field the pages do not state is not
written. Nothing is deleted.

Two models (22 Nov 2026, the first trial: the open model took 42 minutes for one company while the server was busy,
and many minutes when it was not):

    --model local    the open model on the job server (Qwen3 8B): no cost, minutes a company
    --model paid     the reader's paid model: seconds and about a tenth of a US cent a company (two questions, about
                     7,000 tokens read); stops at --max-usd. The whole list of 2,000 companies is about 2 to 3 USD.

    python company_profile.py [--model local|paid] [--limit 20] [--minutes 50] [--threads 8] [--max-usd 3]
                              [--partition 0/1] [--redo-days 180] [--commit]

Dry run reads and prints counts only. The log is public: counts, seconds and shares, never a company, a value or a
title. Env: SUPABASE_DB_URL; HF_HOME for the open model's file; ANTHROPIC_API_KEY for --model paid; AIRTABLE_QA_TOKEN
for the run record.
"""
import argparse, collections, json, os, re, sys, time, zlib

MODEL = "qwen3-8b"
PAGE_CHARS = 2600
PAGES_A_QUESTION = 4
QUESTIONS = {
    "identity": {
        "searches": ["headquarters headquartered registered office incorporated domiciled founded established",
                     "listed listing stock exchange shares ticker shareholders ownership",
                     "subsidiaries subsidiary principal consolidated entities group structure parent",
                     "countries operations presence operates worldwide locations markets"],
        "ask": ("Who the company is. headquarters_city and headquarters_country: where its head office is. country_of_incorporation: where it is "
                "registered. year_founded: a year. listing: the stock exchange and ticker as printed, or \"not listed\". ownership: one of listed, "
                "state-owned, private, cooperative, subsidiary, unknown. parent: the company that controls it, if it has one. subsidiaries: its "
                "main subsidiaries by name, 12 at most. countries: the countries it says it operates in, by name, 40 at most. countries_count: "
                "the number of countries it says it operates in."),
        "fields": {"headquarters_city": "text", "headquarters_country": "text", "country_of_incorporation": "text", "year_founded": "year",
                   "listing": "text", "ownership": ["listed", "state-owned", "private", "cooperative", "subsidiary", "unknown"], "parent": "text",
                   "subsidiaries": "list", "countries": "list", "countries_count": "count"}},
    "activity": {
        "searches": ["segments segment divisions business units principal activities",
                     "products services brands customers clients portfolio",
                     "business model value chain strategy revenue",
                     "industry sector market leading manufacturer producer provider"],
        "ask": ("What the company does. segments: its business segments as it names them, 8 at most. products: its main products or services, "
                "10 at most, a few words each. customers: one of businesses, consumers, both, governments, unknown. sector: the broad sector "
                "in two or three words (for example Technology, Financial services, Food and beverage). industry: the industry in two or three "
                "words (for example Semiconductors, Retail banking, Dairy). subsector: the narrower line of business in two to four words "
                "(for example Analog chips, Infant nutrition). description: what the company does, in 30 words at most."),
        "fields": {"segments": "list", "products": "list", "customers": ["businesses", "consumers", "both", "governments", "unknown"],
                   "sector": "text", "industry": "text", "subsector": "text", "description": "text"}},
}
DOCS_SQL = """
select d.library_id from public.documents d
where d.wba_id = %s and d.text_status = 'ok' and coalesce(d.pages, 0) >= 30 and coalesce(d.year, 0) >= %s
order by d.year desc, (d.doc_type in ('Annual Report', 'Integrated Report', 'Financial Statement', 'Universal Registration Document', 'Form 10-K', 'Form 20-F')) desc,
         d.pages desc limit 6"""
PAGES_SQL = """
select p.library_id, p.page, left(regexp_replace(p.body, '\\s+', ' ', 'g'), %s)
from public.pages p
where p.library_id = any(%s) and p.words @@ %s::tsquery
order by ts_rank(p.words, %s::tsquery) desc, p.library_id, p.page limit %s"""
QUEUE_SQL = """
select c.wba_id from public.companies c
where exists (select 1 from public.documents d where d.wba_id = c.wba_id and d.text_status = 'ok' and coalesce(d.pages, 0) >= 30 and coalesce(d.year, 0) >= %s)
  and not exists (select 1 from public.company_profile_runs r where r.wba_id = c.wba_id and r.at > now() - make_interval(days => %s))
order by md5(c.wba_id)"""


def schema(question):
    props = {}
    for name, kind in QUESTIONS[question]["fields"].items():
        if isinstance(kind, list):
            props[name] = {"type": "string", "enum": kind}
        elif kind == "list":
            props[name] = {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": 40}
        elif kind in ("year", "count"):
            props[name] = {"type": ["integer", "null"]}
        else:
            props[name] = {"type": ["string", "null"], "maxLength": 260}
    props["from"] = {"type": "object", "additionalProperties": {"type": "integer"}}
    return {"type": "object", "required": list(QUESTIONS[question]["fields"]) + ["from"], "additionalProperties": False, "properties": props}


def tsquery(words):
    """The words of a search as one "any of these" query for the words index. Letters only: nothing typed by a person reaches it."""
    ws = [w for w in re.findall(r"[a-z]+", words.lower()) if len(w) >= 3]
    return " | ".join(dict.fromkeys(ws))


def told(company, question, pages):
    """What the model reads: the question, the answer's shape, and the excerpts numbered from 1."""
    q = QUESTIONS[question]
    excerpts = "\n\n".join(f"[{n}] {text}" for n, (_, _, text) in enumerate(pages, 1))
    return (f"These are excerpts of the reports of the company {company}. Answer about {company} itself, from the excerpts only, never from memory. "
            f"{q['ask']} Use null, or an empty list, for anything the excerpts do not state. In \"from\", give for each field you filled the "
            f"number of the excerpt that states it. Reply with JSON only, with exactly these fields: {', '.join(q['fields'])}, from.\n\nExcerpts:\n\n{excerpts}")


def clean(kind, value, year_now):
    """A field's value as it is kept, or None when it says nothing."""
    blank = {"", "null", "none", "n/a", "na", "unknown", "not stated", "not specified", "not mentioned", "not disclosed", "-"}
    if isinstance(kind, list):
        v = str(value or "").strip().lower()
        return v if v in kind and v != "unknown" else None
    if kind == "list":
        out = []
        for x in (value if isinstance(value, list) else []):
            x = re.sub(r"\s+", " ", str(x or "")).strip(" .;,")
            if x and x.lower() not in blank and len(x) <= 80 and x.lower() not in (y.lower() for y in out):
                out.append(x)
        return out[:40] or None
    if kind == "year":
        return value if isinstance(value, int) and not isinstance(value, bool) and 1500 <= value <= year_now else None
    if kind == "count":
        return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 250 else None
    v = re.sub(r"\s+", " ", str(value or "")).strip(" .;,")
    return v[:260] if v and v.lower() not in blank else None


def rows_of(question, reply, pages, year_now):
    """[(field, value, library_id, page)] for the fields the reply fills from an excerpt that exists. A field with no
    excerpt named, or an excerpt number that was not shown, is left out: a value with no page cannot be checked."""
    try:
        data = json.loads(reply)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    src = data.get("from") if isinstance(data.get("from"), dict) else {}
    out = []
    for name, kind in QUESTIONS[question]["fields"].items():
        value = clean(kind, data.get(name), year_now)
        n = src.get(name)
        if value is None or not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= len(pages):
            continue
        out.append((name, value, pages[n - 1][0], pages[n - 1][1]))
    return out


def pick_pages(cur, docs, question):
    """The pages a question is shown: the best of each search in turn, each page once, PAGES_A_QUESTION at most."""
    found, seen = [], set()
    per = [[] for _ in QUESTIONS[question]["searches"]]
    for i, words in enumerate(QUESTIONS[question]["searches"]):
        q = tsquery(words)
        cur.execute(PAGES_SQL, (PAGE_CHARS, docs, q, q, 3))
        per[i] = cur.fetchall()
    for rank in range(3):
        for rows in per:
            if rank < len(rows) and (rows[rank][0], rows[rank][1]) not in seen and len(found) < PAGES_A_QUESTION:
                seen.add((rows[rank][0], rows[rank][1])); found.append(rows[rank])
    return found


def json_of(reply):
    """The JSON object in a reply that may have words around it (the paid model is asked for JSON, not held to it)."""
    reply = reply or ""
    a, b = reply.find("{"), reply.rfind("}")
    return reply[a:b + 1] if 0 <= a < b else reply


def paid_model():
    """(ask(text, schema) -> reply, spent() -> USD) with the reader's paid model."""
    import model_reader
    usage = collections.Counter()
    system = "You read excerpts of a company's own reports and reply with one JSON object and nothing else. You use only what the excerpts state."

    def ask(text, sch):
        return json_of(model_reader.call([{"role": "user", "content": text}], usage, system=system, max_tokens=900))
    return ask, lambda: model_reader.cost(usage)


def in_partition(wba_id, partition):
    k, n = (int(x) for x in partition.split("/"))
    return zlib.crc32(wba_id.encode("utf-8")) % n == k


def local_model(threads):
    from huggingface_hub import hf_hub_download
    from llama_cpp import Llama
    import doc_label_trial as T
    repo, file, suffix = T.MODELS[MODEL]
    llm = Llama(model_path=hf_hub_download(repo, file), n_ctx=8192, n_threads=threads, verbose=False)
    return lambda text, sch: llm.create_chat_completion(messages=[{"role": "user", "content": text + suffix}],
                                                         response_format={"type": "json_object", "schema": sch}, temperature=0,
                                                         max_tokens=700)["choices"][0]["message"]["content"] or ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=["local", "paid"], default="local")
    ap.add_argument("--max-usd", type=float, default=3.0, help="the paid model stops at this cost")
    ap.add_argument("--limit", type=int, default=20, help="companies this run")
    ap.add_argument("--minutes", type=int, default=50, help="stop cleanly after this many minutes")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--partition", default="0/1", help="k/n: this run takes the companies of slice k of n")
    ap.add_argument("--redo-days", type=int, default=180, help="a company read less than this many days ago is not read again")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import qa, evidence_db
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("company_profile.py")
    c = collections.Counter(); t0 = time.time(); year_now = time.gmtime().tm_year; filled = collections.Counter()
    with evidence_db.connect() as conn:
        cur = conn.cursor()
        cur.execute("select to_regclass('public.company_profile') is not null and to_regclass('public.company_profile_runs') is not null")
        if not cur.fetchone()[0]:
            print("public.company_profile is not there yet: nothing to do.")
            run.finish("ok", rows_in=0, rows_out=0, notes="no table"); return 0
        cur.execute(QUEUE_SQL, (year_now - 3, a.redo_days))
        queue = [r[0] for r in cur.fetchall() if in_partition(r[0], a.partition)]
        todo = queue[:a.limit]
        print(f">>> {len(queue):,} companies wait in slice {a.partition}; {len(todo)} this run", flush=True)
        if not todo:
            run.finish("ok", rows_in=0, rows_out=0, notes="nothing waiting"); return 0
        if a.model == "paid":
            ask, spent = paid_model()
        else:
            ask, spent = local_model(a.threads), (lambda: 0.0)
        name_of_model = MODEL if a.model == "local" else (os.environ.get("READER_MODEL") or __import__("model_reader").DEFAULT_MODEL)
        for wba_id in todo:
            if time.time() - t0 > a.minutes * 60:
                c["stopped at the time cap"] = 1; break
            if spent() >= a.max_usd:
                c["stopped at the cost cap"] = 1; break
            t1 = time.time()
            cur.execute("select name from public.companies where wba_id = %s", (wba_id,)); name = cur.fetchone()[0]
            cur.execute(DOCS_SQL, (wba_id, year_now - 3)); docs = [r[0] for r in cur.fetchall()]
            rows = []
            for question in QUESTIONS:
                pages = pick_pages(cur, docs, question)
                if not pages:
                    c[f"{question}: no page"] += 1; continue
                try:
                    reply = ask(told(name, question, pages), schema(question))
                except Exception as e:                              # one company that fails does not cost the others
                    c[f"model failed: {type(e).__name__}"] += 1; continue
                got = rows_of(question, reply, pages, year_now)
                if not got:
                    c[f"{question}: nothing read"] += 1
                rows += got
            c["companies"] += 1; c["fields"] += len(rows)
            for f, *_ in rows:
                filled[f] += 1
            if a.commit:
                for field, value, lib, page in rows:
                    cur.execute("""insert into public.company_profile (wba_id, field, value, library_id, page, model, read_at, status)
                                   values (%s, %s, %s::jsonb, %s, %s, %s, now(), 'read')
                                   on conflict (wba_id, field) do update set value = excluded.value, library_id = excluded.library_id, page = excluded.page,
                                     model = excluded.model, read_at = now()
                                   where public.company_profile.status = 'read'""", (wba_id, field, json.dumps(value, ensure_ascii=False), lib, page, name_of_model))
                cur.execute("insert into public.company_profile_runs (wba_id, fields, seconds, model) values (%s, %s, %s, %s)", (wba_id, len(rows), time.time() - t1, name_of_model))
                conn.commit()
            print(f"    company {c['companies']}: {len(rows)} fields in {time.time() - t1:.0f} s", flush=True)
    n = max(c["companies"], 1)
    print(f">>> {c['companies']} companies, {c['fields']} fields ({c['fields'] / n:.1f} a company), {(time.time() - t0) / n:.0f} s a company, {spent():.3f} USD ({a.model}); " + ("written" if a.commit else "dry run"))
    print("    filled: " + ", ".join(f"{f} {filled[f]}" for q in QUESTIONS.values() for f in q["fields"]))
    for k, v in sorted(c.items()):
        if k not in ("companies", "fields"):
            print(f"    {k}: {v}")
    run.finish("ok", rows_in=len(todo), rows_out=c["fields"] if a.commit else 0, counters={**{k: v for k, v in c.items()}, **{f"filled_{f}": v for f, v in filled.items()}, "usd_cents": round(spent() * 100)},
               notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
