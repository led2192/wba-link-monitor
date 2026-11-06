#!/usr/bin/env python3
"""
decision_mining.py: which figure do the analysts choose, learned from their validated answers.

The model reader often has the right page in front of it and still picks another figure (6 Nov 2026: the
validated number was on the model's pages for 75% of revenue answers and the reader was right for 62%). The
choice rules are not written down (questions sent to the data team the same day: percentage or amount of
low-carbon capex, average or year-end employees, joint ventures in revenue, total income for banks). This
script reads them off the validated answers, with no model:

  A. what the analysts record: the unit of each validated answer (a percentage or an amount);
  B. when the validated figure is printed on a page of the company's documents for the year (or the next):
     the line it is printed on against the competing lines of the same page (lines that name the attribute,
     with a figure of the same order of size, not the chosen one). Per word, how often the chosen line has it
     and how often a passed-over line has it while the chosen one does not: words the analysts follow
     ("average", "group") and words they pass over ("parent", "excluding"). Also which column of its line
     the chosen figure is in, and whether it is the largest of the competitors;
  C. when it is not printed: whether it is close to a printed figure (within 1%), the sum of two figures of
     a page that names the attribute, the average of two figures on one line, or (for a percentage) one
     figure as a share of another. Each test is also run on the validated value times 1.37, which no analyst
     chose, so the report shows how often a test matches by chance.

The report (public) has counts, shares and single words only. Read-only: the gold bases (GOLD_TOKEN), the
evidence database (SUPABASE_DB_URL). Options: --limit N answers per attribute (default 150), --attributes,
--budget minutes (100).
"""
import argparse, collections, json, math, os, re, sys, time, traceback

import qa
from label_mining import number_spans, words, STOP
from value_eval import printed_forms, scales_for, tsquery_for, numbers_on, attr_words, CURRENCY_SCALES, EMISSION_SCALES

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_PAGES = 12                    # pages per answer
MAX_NUMS = 300                    # figures per answer for the derivation tests
MIN_SUPPORT = 5                   # answers a word needs before it is reported
CHANCE = 1.37                     # the decoy value for the chance rate
TOL = 0.005                       # relative tolerance of the derivation tests
NAMES = {"GMC_02526.1": ["employees", "headcount", "workforce", "staff", "fte"]}   # "total" and "number" name every page
EXTRA_STOP = set("scope emissions revenue revenues employees total capex co2e tco2e".split())


def is_percent(unit):
    u = (unit or "").strip().lower()
    return u.startswith("%") or u.startswith("percent")


def lines_with(body, r, d):
    """The lines of a page on which the printed figure r appears, with the figure's column (1 = first figure)."""
    out = []
    for line in (body or "").split("\n"):
        for s in number_spans(line, r, d):
            before = [m for m in re.finditer(r"\d[\d,.'’ ]*\d|\d", line[:s])]
            out.append((line, len([m for m in before if not re.fullmatch(r"(19|20)\d\d", m.group(0).strip())]) + 1))
            break
    return out


def competitors(body, chosen_lines, names, value_printed):
    """Lines of the page that name the attribute and print a figure of the same order of size (a tenth to ten
    times the chosen one), other than the chosen lines."""
    out = []
    for line in (body or "").split("\n"):
        if line in chosen_lines:
            continue
        low = line.lower()
        if not any(n in low for n in names):
            continue
        nums = [x for x in numbers_on(line) if not (1900 <= x <= 2100 and float(x).is_integer())]
        if any(value_printed / 10 <= x <= value_printed * 10 for x in nums):
            out.append(line)
    return out


def line_words(line):
    return {w for w in words(line) if w not in STOP and w not in EXTRA_STOP and len(w) > 1}


def derivations(v, nums_by_line, percent):
    """Which simple derivations from printed figures give v: 'close' (within 1%), 'sum of two', 'average of
    two on one line', 'share of another' (percent only). nums_by_line: [[figures of a line]] (in the scale
    the value would be printed in)."""
    found = set()
    flat = sorted({x for ln in nums_by_line for x in ln if x > 0})[:MAX_NUMS]
    if any(abs(x - v) / v <= 0.01 for x in flat):
        found.add("close (within 1%)")
    if percent:
        for i, x in enumerate(flat):
            for y in flat[i + 1:]:
                if abs(100 * x / y - v) <= 0.05 + 0.005 * v:
                    found.add("one figure as a share of another"); break
            if "one figure as a share of another" in found:
                break
        return found
    s = set(flat)
    for i, x in enumerate(flat):
        for y in flat[i:]:
            if abs(x + y - v) / v <= TOL:
                found.add("sum of two figures"); break
        if "sum of two figures" in found:
            break
    for ln in nums_by_line:
        ln = [x for x in ln if x > 0]
        if any(abs((ln[i] + ln[j]) / 2 - v) / v <= TOL for i in range(len(ln)) for j in range(i + 1, len(ln))):
            found.add("average of two on one line"); break
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=150)
    ap.add_argument("--attributes", default="")
    ap.add_argument("--budget", type=int, default=100)
    a = ap.parse_args()
    token = os.environ.get("GOLD_TOKEN", "").strip()
    if not token:
        open("decision_mining.md", "w").write("### Decision mining: GOLD_TOKEN is not set; nothing run.\n")
        return 0
    from value_eval import gold_values
    spec = json.load(open(os.path.join(HERE, "attributes.json"), encoding="utf-8"))["attributes"]
    gold = json.load(open(os.path.join(HERE, "gold_attributes.json"), encoding="utf-8"))
    codes = [c for c, v in gold["attributes"].items() if v.get("type") == "number"
             and (not a.attributes or c in a.attributes.split(","))]
    run = qa.Run("decision_mining.py")
    answers = [g for base in gold["bases"].values() for g in gold_values(token, base, codes) if g["value"]]
    by = collections.defaultdict(list)
    for g in answers:
        by[g["attribute"]].append(g)
    units = {c: collections.Counter(("percentage" if is_percent(g["unit"]) else "amount") for g in by[c]) for c in codes}
    queues = [by[c][:a.limit] if a.limit else by[c] for c in codes]
    todo = [q[i] for i in range(max((len(q) for q in queues), default=0)) for q in queues if i < len(q)]
    print(f">>> {len(todo):,} answers", flush=True)

    st = {c: collections.Counter() for c in codes}
    chosen_w = {c: collections.Counter() for c in codes}     # answers whose chosen line has the word
    passed_w = {c: collections.Counter() for c in codes}     # answers where a passed-over line has it and the chosen one does not
    column = {c: collections.Counter() for c in codes}
    derived = {c: collections.Counter() for c in codes}
    chance = {c: collections.Counter() for c in codes}
    t0, cut = time.time(), False
    with __import__("evidence_db").connect() as conn:
        cur = conn.cursor()
        cur.execute("set statement_timeout = '30s'")
        for i, g in enumerate(todo, 1):
            if time.time() - t0 > a.budget * 60:
                cut = True; break
            if i % 100 == 0:
                print(f"  {i:,}/{len(todo):,} in {(time.time() - t0) / 60:.0f} min", flush=True)
            c = g["attribute"]; s = st[c]; s["answers"] += 1
            percent = is_percent(g["unit"])
            fam = (spec.get(c) or {}).get("unit_family")
            scales = (1,) if percent else (scales_for(fam) if fam else tuple(sorted(set(CURRENCY_SCALES) | set(EMISSION_SCALES))))
            forms = printed_forms(g["value"], scales) if not percent else [(round(g["value"], d), 0, d) for d in (0, 1, 2)]
            names = NAMES.get(c) or attr_words(c, gold) or [w.lower() for w in (spec.get(c) or {}).get("labels", [])[:6]]
            try:
                q = " | ".join(f"({tsquery_for(v, d)})" for v, _, d in forms)
                cur.execute("select p.body from public.pages p join public.documents d using (library_id) "
                            "where d.wba_id = %s and d.year in (%s, %s) and p.words @@ to_tsquery('simple', %s) limit %s",
                            (g["wba"], g["year"], g["year"] + 1, q, MAX_PAGES))
                printed_pages = [b for (b,) in cur.fetchall()]
            except Exception:
                conn.rollback(); cur.execute("set statement_timeout = '30s'")
                s["database did not answer"] += 1; continue
            hit = False
            for body in printed_pages:
                for r, _, d in forms:
                    ls = lines_with(body, r, d)
                    named = [(ln, col) for ln, col in ls if any(n in ln.lower() for n in names)]
                    ls = named if (named or percent) else ls      # a bare "40" is everywhere: a percentage needs its name on the line
                    if not ls:
                        continue
                    hit = True
                    line, col = ls[0]
                    column[c][min(col, 4)] += 1
                    cw = line_words(line)
                    comp = competitors(body, {l for l, _ in ls}, names, r)
                    pw = set().union(*[line_words(x) for x in comp]) - cw if comp else set()
                    chosen_w[c].update(cw); passed_w[c].update(pw)
                    if comp:
                        s["with competing lines"] += 1
                        others = [x for ln in comp for x in numbers_on(ln) if r / 10 <= x <= r * 10]
                        if others and r >= max(others):
                            s["the largest of the competitors"] += 1
                        elif others and r <= min(others):
                            s["the smallest of the competitors"] += 1
                    break
                if hit:
                    break
            if hit:
                s["printed"] += 1
                continue
            # C: not printed. Figures of the pages that name the attribute, in the value's printing scale.
            try:
                tq = " | ".join("(" + " <-> ".join(re.findall(r"\w+", n)) + ")" for n in names if re.findall(r"\w+", n))
                cur.execute("select p.body from public.pages p join public.documents d using (library_id) "
                            "where d.wba_id = %s and d.year in (%s, %s) and p.words @@ to_tsquery('simple', %s) "
                            "order by ts_rank(p.words, to_tsquery('simple', %s)) desc limit %s",
                            (g["wba"], g["year"], g["year"] + 1, tq, tq, MAX_PAGES))
                label_pages = [b for (b,) in cur.fetchall()]
            except Exception:
                conn.rollback(); cur.execute("set statement_timeout = '30s'")
                s["database did not answer"] += 1; continue
            if not label_pages:
                s["not printed, no page names the attribute"] += 1; continue
            s["not printed"] += 1
            by_line = [numbers_on(ln) for body in label_pages for ln in body.split("\n")]
            by_line = [[x for x in ln if not (1900 <= x <= 2100 and float(x).is_integer())] for ln in by_line]
            for scale in (scales if not percent else (1,)):
                v = g["value"] / scale
                if v < 1 and not percent:
                    continue
                got = derivations(v, by_line, percent)
                fake = derivations(v * CHANCE, by_line, percent)
                if got or fake:
                    for k in got:
                        derived[c][k] += 1
                    for k in fake:
                        chance[c][k] += 1
                    break

    md = ["### Which figure the analysts choose (learned from the validated answers)", "",
          f"{sum(s['answers'] for s in st.values()):,} validated answers" + (f", at most {a.limit} per attribute" if a.limit else "")
          + ("; stopped at the time budget" if cut else "") + ". No model. Counts, shares and single words only.", ""]
    for c in codes:
        s = st[c]; n = s["answers"] or 1
        name = (spec.get(c) or gold["attributes"][c]).get("name")
        u = units[c]; un = sum(u.values()) or 1
        md += [f"#### {c} {name}", "",
               f"What the analysts record (all {un:,} validated answers): amounts {u['amount'] / un:.0%}, percentages {u['percentage'] / un:.0%}.", "",
               f"Of {s['answers']:,} answers read: printed on a page of the year's documents {s['printed']:,} ({s['printed'] / n:.0%}); "
               f"not printed but pages name the attribute {s['not printed']:,} ({s['not printed'] / n:.0%}); "
               f"no page names it {s['not printed, no page names the attribute']:,}.", ""]
        p = s["printed"] or 1
        cols = column[c]
        md.append(f"Where printed: the figure's column on its line: first {cols[1] / p:.0%}, second {cols[2] / p:.0%}, third {cols[3] / p:.0%}, "
                  f"later {cols[4] / p:.0%}. With competing lines on the page: {s['with competing lines']:,}; "
                  f"of those the chosen figure is the largest {s['the largest of the competitors']:,} and the smallest {s['the smallest of the competitors']:,}.")
        rows = []
        for w in set(chosen_w[c]) | set(passed_w[c]):
            k1, k2 = chosen_w[c][w], passed_w[c][w]
            if k1 + k2 < MIN_SUPPORT:
                continue
            rows.append((w, k1, k2, (k1 + 1) / (k2 + 1)))
        follow = sorted([r for r in rows if r[3] >= 3], key=lambda r: -r[1])[:12]
        avoid = sorted([r for r in rows if r[3] <= 1 / 3], key=lambda r: -r[2])[:12]
        if follow or avoid:
            md += ["", "| Words on the chosen line (followed) | Chosen | Passed over | | Words on passed-over lines (avoided) | Chosen | Passed over |",
                   "| --- | --: | --: | --- | --- | --: | --: |"]
            for k in range(max(len(follow), len(avoid))):
                f = follow[k] if k < len(follow) else ("", "", "", "")
                v = avoid[k] if k < len(avoid) else ("", "", "", "")
                md.append(f"| {f[0]} | {f[1]} | {f[2]} | | {v[0]} | {v[1]} | {v[2]} |")
        nn = s["not printed"] or 1
        if s["not printed"]:
            md += ["", f"Not printed ({s['not printed']:,}): " + ", ".join(
                f"{k} {derived[c][k]:,} ({derived[c][k] / nn:.0%}; by chance {chance[c][k] / nn:.0%})"
                for k in ["close (within 1%)", "sum of two figures", "average of two on one line", "one figure as a share of another"]
                if derived[c][k] or chance[c][k]) + "."]
        md.append("")
    open("decision_mining.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    run.finish("ok", rows_in=len(todo), rows_out=sum(s["printed"] for s in st.values()), counters={c: dict(st[c]) for c in codes})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("decision_mining.md", "w").write("### Decision mining: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
