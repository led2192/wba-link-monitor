#!/usr/bin/env python3
"""
paired_stats.py: is option B really better than option A on the same answers?

Our comparisons score two options on the same validated answers. Looking at two shares (58% against 60%) hides
two things: the answers are paired (each one is right or wrong under both options), and answers of the same
company are not independent (a clean report gets most of its figures right under any option, a scanned one
most wrong). Since 7 Nov 2026 every comparison reports, besides the two shares:

  fixed / broken   answers B gets right and A wrong, and the reverse (the regressions to look at);
  McNemar p        the exact two-sided binomial test on fixed against broken (only discordant answers count);
  95% interval     of B minus A in points, by a bootstrap that resamples companies, not answers.

The CLAUDE.md rule (no attribute worse by more than 3 points) is still the rule for adopting an option; this
module says whether a gain or a loss is distinguishable from chance on the sample at hand.
"""
import math, random

BOOT = 2000
SEED = 11


def mcnemar_p(fixed, broken):
    """Exact two-sided McNemar p (binomial, p = 1/2 on the discordant answers). 1.0 with none."""
    n = fixed + broken
    if n == 0:
        return 1.0
    k = min(fixed, broken)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def cluster_bootstrap(rows, boot=BOOT, seed=SEED):
    """rows: (cluster, a_right, b_right). The 95% interval of (B - A) share, in points, resampling clusters
    with replacement. (delta, low, high); (0, 0, 0) with no rows."""
    if not rows:
        return 0.0, 0.0, 0.0
    by = {}
    for c, a, b in rows:
        s = by.setdefault(c, [0, 0, 0])
        s[0] += 1; s[1] += bool(a); s[2] += bool(b)
    groups = list(by.values())
    n = sum(g[0] for g in groups)
    delta = 100.0 * (sum(g[2] for g in groups) - sum(g[1] for g in groups)) / n
    rnd = random.Random(seed)
    ds = []
    for _ in range(boot):
        pick = [groups[rnd.randrange(len(groups))] for _ in groups]
        m = sum(g[0] for g in pick)
        ds.append(100.0 * (sum(g[2] for g in pick) - sum(g[1] for g in pick)) / m)
    ds.sort()
    return delta, ds[int(0.025 * boot)], ds[min(boot - 1, int(0.975 * boot))]


def compare(rows):
    """A summary dict of paired rows (cluster, a_right, b_right)."""
    n = len(rows)
    fixed = sum(1 for _, a, b in rows if b and not a)
    broken = sum(1 for _, a, b in rows if a and not b)
    d, lo, hi = cluster_bootstrap(rows)
    return {"n": n, "companies": len({c for c, _, _ in rows}), "a": sum(bool(a) for _, a, _ in rows) / n if n else 0.0,
            "b": sum(bool(b) for _, _, b in rows) / n if n else 0.0, "fixed": fixed, "broken": broken,
            "p": mcnemar_p(fixed, broken), "delta": d, "low": lo, "high": hi}


def verdict(s):
    """Plain words: better, worse, or not distinguishable from chance on this sample."""
    if s["n"] == 0:
        return "no paired answers"
    if s["low"] > 0 and s["p"] < 0.05:
        return "better"
    if s["high"] < 0 and s["p"] < 0.05:
        return "worse"
    return "not distinguishable from chance"


HEADER = ["| Comparison | Paired answers | Companies | A right | B right | Fixed by B | Broken by B | B - A (points) | 95% interval, by company | McNemar p | Reading |",
          "| --- | --: | --: | --: | --: | --: | --: | --: | --- | --: | --- |"]


def row(label, rows):
    s = compare(rows)
    return (f"| {label} | {s['n']:,} | {s['companies']:,} | {s['a']:.0%} | {s['b']:.0%} | {s['fixed']:,} | {s['broken']:,} | "
            f"{s['delta']:+.1f} | {s['low']:+.1f} to {s['high']:+.1f} | {s['p']:.3f} | {verdict(s)} |")
