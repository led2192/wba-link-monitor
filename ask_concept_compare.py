#!/usr/bin/env python3
"""
ask_concept_compare.py: does option ask_report_words give better answers to questions about a kind of content?

"main difficulties <company> in 2025" was answered from notes to the accounts: reports do not say "difficulties",
they say risks, challenges, headwinds, outlook. The option has the answer read up to three pages found by the
reports' own words for what is asked (public.ask_concepts). It changes which pages an answer reads, so it is
compared before it is switched on (the rule of the house).

Each case is a question about one company (the fixed list below: everyday words for difficulties, risks, strategy,
outlook, reasons and changes, with and without a year). The database asks it twice (public.ask_pages_trial): the
option off and on. Compared: how many answers begin by saying the pages do not hold it ("hedged", "not stated"),
their length, the pages they cite, and how many of the eight pages changed. Nothing is kept.

It CALLS THE PAID READER: two calls a question, about 0.4 US cents a question at 8,000 characters a page; the owner
allowed it on 23 Nov 2026 (about 0.35 USD). The log is public: it prints counts and shares only, never a question's
answer or a value.

    python ask_concept_compare.py [--option ask_report_words] [--limit 40]

Env: SUPABASE_DB_URL; AIRTABLE_QA_TOKEN for the run record (QA_COMMIT).
"""
import argparse, os, sys

import qa

COMPANIES = ("Nike", "Unilever", "Nestle", "Shell", "Inditex", "Iberdrola", "BBVA", "Danone", "Toyota", "Coca-Cola")
ASKS = (("main difficulties", True), ("biggest challenges", False), ("key risks", True), ("strategy priorities", False),
        ("outlook for next year", False), ("why did revenue change", True), ("what changed in the emissions methodology", False), ("main problems", True))
PRICE_IN, PRICE_OUT = 0.10, 0.50          # USD a million tokens, the reader's list price (model_reader.cost)


def cases(limit, companies=COMPANIES):
    """[(words, company, with_year)]: each question of ASKS about a different company, in turn."""
    out = []
    for i in range(limit):
        words, with_year = ASKS[i % len(ASKS)]
        out.append((words, companies[(i + i // len(ASKS)) % len(companies)], with_year))
    return out


def summarise(trials):
    """Counts over the trials that gave both answers."""
    both = [t for t in trials if isinstance(t.get("off"), dict) and isinstance(t.get("on"), dict)]
    c = {"asked": len(trials), "compared": len(both), "skipped": len(trials) - len(both)}
    for arm in ("off", "on"):
        c[f"hedged_{arm}"] = sum(1 for t in both if t[arm].get("hedged") or t[arm].get("not_stated"))
        c[f"chars_{arm}"] = round(sum(t[arm].get("chars", 0) for t in both) / len(both)) if both else 0
        c[f"cites_{arm}"] = round(sum(t[arm].get("cites", 0) for t in both) / len(both), 1) if both else 0
    c["better"] = sum(1 for t in both if (t["off"].get("hedged") or t["off"].get("not_stated")) and not (t["on"].get("hedged") or t["on"].get("not_stated")))
    c["worse"] = sum(1 for t in both if not (t["off"].get("hedged") or t["off"].get("not_stated")) and (t["on"].get("hedged") or t["on"].get("not_stated")))
    c["pages_changed"] = round(sum(len(set(t["on"].get("pages", [])) - set(t["off"].get("pages", []))) for t in both) / len(both), 1) if both else 0
    c["same_pages"] = sum(1 for t in both if set(t["on"].get("pages", [])) == set(t["off"].get("pages", [])))
    # option ask_source_entity: the questions that read a page of a subsidiary's report, and the answers that name the subsidiary
    c["marked"] = sum(1 for t in both if t["on"].get("marked"))
    for arm in ("off", "on"):
        c[f"named_{arm}"] = sum(1 for t in both if t["on"].get("marked") and t[arm].get("named"))
    c["tokens_in"] = sum(t.get("tokens_in", 0) for t in trials); c["tokens_out"] = sum(t.get("tokens_out", 0) for t in trials)
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--option", default="ask_report_words")
    ap.add_argument("--limit", type=int, default=40)
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import evidence_db
    run = qa.Run("ask_concept_compare.py")
    trials = []
    with evidence_db.connect() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("select coalesce(max(d.year), extract(year from now())::int) from public.documents d where d.doc_type = 'Annual Report' and d.text_status = 'ok'"); year = cur.fetchone()[0] - 1
            companies = COMPANIES
            if a.option == "ask_source_entity":          # judged on the companies that hold most annual reports of a subsidiary; no year, so those are met
                cur.execute("select d.company from public.documents d join public.document_entities e on e.library_id = d.library_id "
                            "where d.doc_type = 'Annual Report' and d.text_status = 'ok' and d.company is not null group by d.company order by count(*) desc, d.company limit 10")
                companies = tuple(r[0] for r in cur.fetchall()) or COMPANIES
            for n, (words, company, with_year) in enumerate(cases(a.limit, companies), 1):
                try:
                    cur.execute("select public.ask_pages_trial(%s, %s, %s, %s, %s)", (words, company, year if with_year else None, year if with_year else None, a.option))
                    trials.append(cur.fetchone()[0])
                except Exception as e:                                   # one question that fails does not cost the others
                    trials.append({"skipped": type(e).__name__})
                if n % 10 == 0:
                    print(f"  {n} questions asked", flush=True)
    c = summarise(trials)
    usd = c["tokens_in"] / 1e6 * PRICE_IN + c["tokens_out"] / 1e6 * PRICE_OUT
    print(f"### Option {a.option}: answers to questions about a kind of content, off and on\n")
    print(f"{c['asked']} questions, {c['compared']} answered both ways ({c['skipped']} skipped).")
    print(f"Answers that begin by saying the pages do not hold it: {c['hedged_off']} off, {c['hedged_on']} on. Better with the option: {c['better']}; worse: {c['worse']}.")
    print(f"Average answer: {c['chars_off']} characters and {c['cites_off']} pages cited off, {c['chars_on']} and {c['cites_on']} on.")
    print(f"Pages read: {c['pages_changed']} of eight are new with the option on average; {c['same_pages']} questions read the same eight.")
    print(f"Questions that read a page of a subsidiary's report: {c['marked']}; of those, answers that name the subsidiary: {c['named_off']} off, {c['named_on']} on.")
    print(f"Cost: {usd:.3f} USD ({c['tokens_in']:,} tokens in, {c['tokens_out']:,} out).", flush=True)
    run.finish("ok", rows_in=c["asked"], rows_out=0, counters={k: v for k, v in c.items()}, notes=f"option {a.option}; {usd:.3f} USD")
    return 0


if __name__ == "__main__":
    sys.exit(main())
