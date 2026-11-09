#!/usr/bin/env python3
"""
compare_runs.py: a paired comparison of two Generic reader runs on the same answers.

extract_eval.py prints, since 7 Nov 2026, one line per answer between OUTCOMES_JSON markers in its log: a hashed
answer key, a hashed company key, the attribute and whether the answer was right (plus whether the figure was in
the documents and on the ranked pages). This reads the logs of two runs (gh run view --log), keeps the answers
both runs scored, and per attribute reports the two shares, the answers fixed and broken by run B, the exact
McNemar p and a 95% interval of B minus A that resamples companies (paired_stats.py).

    python compare_runs.py RUN_A RUN_B [--repo owner/name]        (writes compare_runs.md)

Answers only one run scored are counted and left out: a share on different answers is not a comparison.
"""
import argparse, collections, json, subprocess, sys

import paired_stats as P

BEGIN, END = "OUTCOMES_JSON_BEGIN", "OUTCOMES_JSON_END"


def outcomes_from_log(text):
    """The OUTCOMES block of a run's log text, or None."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if BEGIN in line and i + 1 < len(lines):
            body = lines[i + 1]
            return json.loads(body[body.index("{"):])
    return None


def fetch(run_id, repo):
    out = subprocess.run(["gh", "run", "view", str(run_id), "--log", "--repo", repo], capture_output=True, text=True)
    if out.returncode:
        raise SystemExit(f"gh run view {run_id}: {out.stderr.strip()[:300]}")
    got = outcomes_from_log(out.stdout)
    if got is None:
        raise SystemExit(f"run {run_id} has no {BEGIN} block (runs before 7 Nov 2026 do not print one)")
    return got


def describe(o):
    return ", ".join(f"{k} {o.get(k)}" for k in ("reader", "select", "order", "guidance", "page_text", "unstated_basis", "period_check", "bm25_pool", "selector", "model_pages", "heldout", "auto_spec", "spec_by", "anchor", "frequency", "reads", "spec_examples", "slots", "escalate", "escalate_steps") if o.get(k) is not None)


def report(a, b, name_a="A", name_b="B"):
    ka = {r[0]: r for r in a["rows"]}
    kb = {r[0]: r for r in b["rows"]}
    both = [k for k in ka if k in kb]
    by = collections.defaultdict(list)
    for k in both:
        ra, rb = ka[k], kb[k]
        by[ra[2]].append((ra[1], ra[3], rb[3]))
    md = [f"### Paired comparison: run {name_a} ({describe(a)}) against run {name_b} ({describe(b)})", "",
          f"Answers scored by both runs: {len(both):,}; only by {name_a}: {len(ka) - len(both):,}; only by {name_b}: {len(kb) - len(both):,} (left out).", ""]
    md += P.HEADER + [P.row(attr, rows) for attr, rows in sorted(by.items())]
    md += [P.row("All attributes", [r for rows in by.values() for r in rows])]
    return "\n".join(md) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_a")
    ap.add_argument("run_b")
    ap.add_argument("--repo", default="led2192/wba-link-monitor")
    x = ap.parse_args()
    md = report(fetch(x.run_a, x.repo), fetch(x.run_b, x.repo), x.run_a, x.run_b)
    open("compare_runs.md", "w").write(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
