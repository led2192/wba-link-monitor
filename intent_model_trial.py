#!/usr/bin/env python3
"""
intent_model_trial.py: can a model read what a person typed better than the page's rules, and at what price?

The intent exam (intent_exam.py) says which way a typed text should take: catalogue, fact, question, overview or
search. The page decides it with rules. This trial gives the same 69 variants (and the 32 real cases) to a model and
counts three things for each model:
  alone       the model decides every text;
  after rules the rules decide, and the model is asked only where the rules fall back to a plain search with a
              company named (the texts the rules do not recognise). This is how it would be used: no call, no wait
              and no cost for the texts the rules know;
  the price   seconds a text, and dollars for the paid model.

The model is told what the rules already found (the company, the years, the kind of report), and the five ways with
one line each. It is not shown the exam's answers and no example is taken from the exam.

    python intent_model_trial.py --paid [--max-usd 0.10]                 the reader's paid model (ANTHROPIC_API_KEY)
    python intent_model_trial.py --models qwen3-4b,qwen3-8b [--threads 8]   open models on the job server (no cost)

Reads tests/use_cases.json, tests/use_case_variants.json and site/search.html. Writes nothing. Prints counts,
seconds and dollars only (the texts of the exam are in the repository already; nothing of a user is read).
Needs node; for --models, llama-cpp-python and huggingface_hub (the job server's /srv/lantern/venv-llm).
"""
import argparse, collections, json, os, re, statistics, sys, time

import intent_exam as E

WAYS = E.WAYS
SYSTEM = """You route what a person typed into the search box of a library of company reports (annual reports, sustainability reports, policies) of about 2,000 large companies. Decide which ONE of five ways the text should take.

catalogue: the person wants to know which documents or years the library holds for a company, not what the documents say.
fact: the person wants one specific figure or named fact about one company (a number, a date, a name, a place, a yes-or-no status), with or without a year.
question: the person asks something that needs an answer in sentences: whether a company does something, how, why, a comparison, an explanation.
overview: the person names one company and a broad subject (environment, climate, workforce, human rights, governance, waste, safety, supply chain...) and wants to know how the company stands on it.
search: the person wants to find pages or a document: a kind of report, an exact phrase in quotes, words with no company, or several loose words to look up.

You are also told what was already recognised in the text. Reply with JSON only: {"way": "<one of catalogue, fact, question, overview, search>"}"""


def describe(case, got):
    """One text as the model reads it: the text, then what the rules recognised."""
    known = []
    known.append("a company is named" if (got.get("company") or got.get("typo")) else "no company is named")
    if got.get("from"):
        known.append(f"years {got['from']}" + (f" to {got['to']}" if got.get("to") and got["to"] != got["from"] else ""))
    if got.get("type"):
        known.append(f"a kind of report is named ({got['type']})")
    return f"Typed: {case['typed']}\nRecognised: {'; '.join(known)}."


def parse_reply(text):
    m = re.search(r'"way"\s*:\s*"([a-z]+)"', text or "")
    return m.group(1) if m and m.group(1) in WAYS else None


def unrecognised(got):
    """Where the model would be asked in real use: the rules fell back to a plain search, a company is named, and no
    kind of report nor quoted phrase says that pages are what is wanted."""
    return got["intent"] == "search" and bool(got.get("company") or got.get("typo")) and not got.get("type") and '"' not in (got.get("q") or "")


def score(cases, got, replies):
    """{'alone': [right, of], 'rules': n right, 'after': n right, 'asked': texts the model would be asked,
    'unanswered': replies that were not one of the five ways, 'by_way': {way: [alone right, after right, of]}}"""
    r = dict(alone=[0, len(cases)], rules=0, after=0, asked=0, unanswered=0, by_way={w: [0, 0, 0] for w in WAYS}, changed=collections.Counter())
    for c, g, m in zip(cases, got, replies):
        want = c["intent"]
        r["unanswered"] += m is None
        r["alone"][0] += m == want
        r["rules"] += g["intent"] == want
        ask = unrecognised(g)
        r["asked"] += ask
        final = (m or g["intent"]) if ask else g["intent"]
        r["after"] += final == want
        b = r["by_way"][want]; b[0] += m == want; b[1] += final == want; b[2] += 1
        if ask and final != g["intent"]:
            r["changed"]["made right" if final == want else "made wrong" if g["intent"] == want else "wrong either way"] += 1
    r["changed"] = dict(r["changed"])
    return r


def table(name, sets, secs, usd):
    out = []
    for label, r in sets:
        n = r["alone"][1]
        out.append(f"| {name} | {label} | {r['rules']} of {n} | {r['alone'][0]} of {n} | {r['after']} of {n} | {r['asked']} | "
                   f"{r['changed'].get('made right', 0)} / {r['changed'].get('made wrong', 0)} | {r['unanswered']} | "
                   f"{statistics.median(secs):.1f} s | {'%.4f' % usd if usd is not None else 'none'} |")
    return out


HEAD = ["| model | texts | rules alone | model alone | rules, then the model where they do not recognise | texts the model is asked | made right / made wrong | not answered as asked | time a text (median) | USD |",
        "|---|---|---|---|---|---|---|---|---|---|"]


def load_sets():
    """[(label, cases, what the page reads)] for the variants and the real cases."""
    out = []
    for label, file in (("69 variants", "use_case_variants.json"), ("32 real cases", "use_cases.json")):
        data = json.load(open(os.path.join(E.HERE, "tests", file), encoding="utf-8"))
        out.append((label, data["cases"], E.read_by_page(data)))
    return out


def run_model(ask, sets, stop=lambda: False):
    """ask(text) -> reply text. Returns ([(label, score)], seconds a text)."""
    scored, secs = [], []
    for label, cases, got in sets:
        replies = []
        for c, g in zip(cases, got):
            if stop():
                replies.append(None); continue
            t = time.time()
            try:
                replies.append(parse_reply(ask(describe(c, g))))
            except Exception as e:
                if type(e).__name__ == "ModelRefused":
                    raise
                replies.append(None)
            secs.append(time.time() - t)
        scored.append((label, score(cases, got, replies)))
    return scored, secs or [0.0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paid", action="store_true", help="the reader's paid model")
    ap.add_argument("--max-usd", type=float, default=0.10)
    ap.add_argument("--models", default="", help="open models on the job server, comma-separated (see fact_classify_trial.MODELS)")
    ap.add_argument("--threads", type=int, default=8)
    a = ap.parse_args()
    if not 0 < a.max_usd <= 0.5:
        sys.exit("--max-usd above 0 and at most 0.5.")
    sets = load_sets()
    md = ["### Reading what is typed: a model beside the page's rules (a trial: nothing here is in use)", "",
          f"Instructions of {len(SYSTEM):,} characters; the model is told what the rules recognised and replies with one word.", ""] + HEAD
    if a.paid:
        if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
            print("ANTHROPIC_API_KEY is not set; the paid model was not run."); return 0
        import model_reader as M
        usage = collections.Counter()
        ask = lambda text: M.call([{"role": "user", "content": text}], usage, system=SYSTEM, max_tokens=30)
        scored, secs = run_model(ask, sets, stop=lambda: M.cost(usage) >= a.max_usd)
        md += table(os.environ.get("READER_MODEL") or M.DEFAULT_MODEL, scored, secs, M.cost(usage))
        md += ["", f"Paid model: {usage['calls']} calls, {usage['input_tokens']:,} tokens in, {usage['output_tokens']:,} out, {M.cost(usage):.4f} USD (stops at {a.max_usd})."]
    names = [m.strip() for m in a.models.split(",") if m.strip()]
    if names:
        from fact_classify_trial import MODELS
        from huggingface_hub import hf_hub_download
        from llama_cpp import Llama
        for name in names:
            if name not in MODELS:
                md.append(f"| {name} | unknown model | | | | | | | | |"); continue
            repo, file, suffix = MODELS[name]
            try:
                t0 = time.time(); llm = Llama(model_path=hf_hub_download(repo, file), n_ctx=2048, n_threads=a.threads, verbose=False); load = time.time() - t0
            except Exception as e:
                md.append(f"| {name} | not loaded: {type(e).__name__} | | | | | | | | |"); continue
            ask = lambda text: llm.create_chat_completion(messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": text + suffix}],
                                                          response_format={"type": "json_object"}, temperature=0, max_tokens=30)["choices"][0]["message"]["content"]
            scored, secs = run_model(ask, sets)
            md += table(name, scored, secs, None)
            md.append(f"| {name} | loaded in {load:.0f} s, {a.threads} threads | | | | | | | | |")
            del llm
    print("\n".join(md))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        open(summary, "a", encoding="utf-8").write("\n".join(md) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
