#!/usr/bin/env python3
"""
fact_classify_trial.py: can a small open model, run on the job server at no cost, classify the facts of answers?

Matching a fact to a metric by its name's words reaches about a third of the facts (15 Nov 2026: 37 of 123), and
words cannot tell "the figure" from "the figure as first reported", "the reason for the restatement" or "the change
in percent". This script tries open models on the server's processors (no graphics card, no API, no money): each
fact is shown to the model with the list of metrics, and the model answers which metric it is (or none), what the
fact is to that metric, and a short generic name when no metric fits.

    role   value        the metric's figure or answer for the period
           restated     the same, given again and changed by a later report
           original     the same, as first reported before a restatement
           change       a difference or a rate of change of the metric, not the metric
           explanation  a reason, a method, a description
           other        anything else

It is a trial, not a classifier in use: nothing it writes is read by the search or by the facts pages. With --commit
each answer goes to public.fact_trials, where the owner can judge the models side by side; without it nothing is
written. The log is public: it prints counts, shares and seconds only, never a fact's name, value or company.

    python fact_classify_trial.py [--models qwen3-4b,qwen3-8b,smollm3-3b] [--limit 30] [--threads 8] [--commit]

Env: SUPABASE_DB_URL; HF_HOME for where the model files are kept. Needs llama-cpp-python and huggingface_hub (the
workflow installs them in an environment of their own, /srv/lantern/venv-llm, apart from the one the other jobs use).
"""
import argparse, json, os, statistics, sys, time

# Only files published by the model's own maker or by the llama.cpp project, all under the Apache 2.0 licence
# (checked on 15 Nov 2026, run 43762773153): repository, file, and what the model needs to answer at once.
MODELS = {
    "qwen3-4b": ("Qwen/Qwen3-4B-GGUF", "Qwen3-4B-Q4_K_M.gguf", " /no_think"),
    "qwen3-8b": ("Qwen/Qwen3-8B-GGUF", "Qwen3-8B-Q4_K_M.gguf", " /no_think"),
    "smollm3-3b": ("ggml-org/SmolLM3-3B-GGUF", "SmolLM3-Q4_K_M.gguf", " /no_think"),
}
ROLES = ("value", "restated", "original", "change", "explanation", "other")

FACTS_SQL = """
select f.id, f.what, f.kind, f.value, f.unit, f.answer, f.company, f.metric
from public.ask_facts f
where f.verdict is distinct from 'wrong'
order by f.id desc limit %s"""
METRICS_SQL = """
select m.key, m.label, g.label, b.label
from public.fact_metrics m join public.fact_groups g on g.key = m.grp join public.fact_blocks b on b.key = g.block
where m.status in ('active', 'proposed') order by b.position, g.position, m.label"""


def instructions(metrics):
    """What the model is told once: the task, the roles, and the metrics (key: label, group)."""
    lines = [f"{key}: {label} ({group})" for key, label, group, _ in metrics]
    return ("You file data points taken from company reports under a fixed list of metrics. For the data point given, answer with JSON only: "
            '{"metric": the key of the one metric it belongs to, or "none"; "role": one of value, restated, original, change, explanation, other; '
            '"name": when metric is "none", a short generic English name for it without the company\'s name or a year, else ""}. '
            "role value: it is that metric's figure or answer for a period. restated: the same figure given again, changed, by a later report. "
            "original: the figure as first reported, before a restatement. change: a difference, reduction or rate of change of the metric, not the metric itself. "
            "explanation: a reason, a method or a description. other: anything else. "
            "Choose a metric only when the data point is about exactly that metric; a target is not the metric it aims at, and an offset or a share is not the total. "
            "The metrics:\n" + "\n".join(lines))


def describe(fact):
    """One fact as the model reads it. fact: (id, what, kind, value, unit, answer, company, metric)."""
    _, what, kind, value, unit, answer, company, _ = fact
    shown = " ".join(str(x) for x in (value, unit) if x not in (None, "")) if kind == "number" else (answer or "")[:160]
    return f"Company: {company or 'unknown'}\nData point: {what}\nKind: {kind}\nContent: {shown}"


def schema(keys):
    return {"type": "object", "required": ["metric", "role", "name"], "additionalProperties": False,
            "properties": {"metric": {"type": "string", "enum": list(keys) + ["none"]}, "role": {"type": "string", "enum": list(ROLES)},
                           "name": {"type": "string", "maxLength": 80}}}


def parse_reply(text, keys):
    """(metric or None, role, name) from the model's reply; (None, None, None) when it is not the JSON asked for."""
    try:
        j = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except Exception:
        return None, None, None
    metric, role = j.get("metric"), j.get("role")
    if role not in ROLES or not (metric == "none" or metric in keys):
        return None, None, None
    return (None if metric == "none" else metric), role, (str(j.get("name") or "")[:80] if metric == "none" else "")


def summarise(rows):
    """rows: [(known metric or None, metric or None, role or None, seconds)] for one model -> the numbers printed."""
    ok = [r for r in rows if r[2] is not None]
    known = [r for r in ok if r[0]]
    return {"facts": len(rows), "answered": len(ok), "with_metric": sum(1 for r in ok if r[1]),
            "known": len(known), "agree": sum(1 for r in known if r[1] == r[0]), "other_metric": sum(1 for r in known if r[1] and r[1] != r[0]),
            "roles": {role: sum(1 for r in ok if r[2] == role) for role in ROLES},
            "seconds": round(statistics.median([r[3] for r in rows]), 1) if rows else 0.0, "slowest": round(max([r[3] for r in rows], default=0.0), 1)}


def line(name, s, load_s):
    share = lambda a, b: f"{100 * a / b:.0f}%" if b else "n/a"
    roles = ", ".join(f"{k} {v}" for k, v in s["roles"].items() if v)
    return (f"| {name} | {s['facts']} | {s['answered']} ({share(s['answered'], s['facts'])}) | {s['agree']} of {s['known']} ({share(s['agree'], s['known'])}) | {s['other_metric']} | "
            f"{s['with_metric']} ({share(s['with_metric'], s['answered'])}) | {roles} | {s['seconds']} s, slowest {s['slowest']} s | {load_s:.0f} s |")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="qwen3-4b,qwen3-8b,smollm3-3b")
    ap.add_argument("--limit", type=int, default=30, help="facts, the newest first (a first run is small on purpose)")
    ap.add_argument("--threads", type=int, default=8, help="processors the model may use (the server has 16 and other jobs)")
    ap.add_argument("--commit", action="store_true", help="write each answer to public.fact_trials")
    a = ap.parse_args()
    names = [m.strip() for m in a.models.split(",") if m.strip()]
    unknown = [m for m in names if m not in MODELS]
    if unknown:
        sys.exit(f"unknown model(s): {', '.join(unknown)}; known: {', '.join(MODELS)}")
    import psycopg
    from huggingface_hub import hf_hub_download
    from llama_cpp import Llama
    run = os.environ.get("GITHUB_RUN_ID", "local")
    with psycopg.connect(os.environ["SUPABASE_DB_URL"]) as conn:
        cur = conn.cursor()
        cur.execute(METRICS_SQL); metrics = cur.fetchall()
        cur.execute(FACTS_SQL, (a.limit,)); facts = cur.fetchall()
    keys = [m[0] for m in metrics]
    system, sch = instructions(metrics), schema(keys)
    print(f">>> {len(facts)} facts, {len(metrics)} metrics, {a.threads} threads; instructions of {len(system):,} characters", flush=True)
    md = ["### Facts classified by open models on the job server (a trial: nothing here is in use)", "",
          f"{len(facts)} facts, the newest first; {len(metrics)} metrics; {a.threads} threads. \"Known\" facts are those the name-matching already gave a metric.", "",
          "| model | facts | answered as asked | same metric as the name-matching | another metric | given a metric | roles | time a fact (median) | load |", "|---|---|---|---|---|---|---|---|---|"]
    for name in names:
        repo, file, suffix = MODELS[name]
        try:
            t0 = time.time()
            path = hf_hub_download(repo, file)
            llm = Llama(model_path=path, n_ctx=4096, n_threads=a.threads, verbose=False)
            load_s = time.time() - t0
        except Exception as e:                                      # a model that does not load is a result, not a crash
            md.append(f"| {name} | not loaded: {type(e).__name__} | | | | | | | |")
            print(f">>> {name}: not loaded ({type(e).__name__})", flush=True)
            continue
        rows, out = [], []
        for n, fact in enumerate(facts, 1):
            t = time.time()
            try:
                got = llm.create_chat_completion(messages=[{"role": "system", "content": system}, {"role": "user", "content": describe(fact) + suffix}],
                                                 response_format={"type": "json_object", "schema": sch}, temperature=0, max_tokens=120)
                metric, role, new_name = parse_reply(got["choices"][0]["message"]["content"] or "", keys)
            except Exception:
                metric, role, new_name = None, None, None
            secs = time.time() - t
            rows.append((fact[7], metric, role, secs)); out.append((name, fact[0], metric, role, new_name, secs, run))
            if n % 10 == 0:
                print(f"  {name}: {n} of {len(facts)}", flush=True)
        del llm
        md.append(line(name, summarise(rows), load_s))
        print(">>> " + md[-1], flush=True)
        if a.commit and out:
            with psycopg.connect(os.environ["SUPABASE_DB_URL"]) as conn:
                conn.cursor().executemany("insert into public.fact_trials (model, fact_id, metric, role, name, seconds, run) values (%s, %s, %s, %s, %s, %s, %s)", out)
                conn.commit()
            print(f">>> {name}: {len(out)} answers written to fact_trials", flush=True)
    text = "\n".join(md) + "\n"
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
