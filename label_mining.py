#!/usr/bin/env python3
"""
label_mining.py: the words reports actually print next to an attribute's value, learned from the
validated answers. Suggests labels and document types for attributes.json; changes nothing itself.

The labels in attributes.json were written by hand, and for low-carbon capex none of them leads to the
pages that hold the value (0 % of answers on the ranked pages, 29 Oct 2026), while the value is in that
year's documents for 80 %. Instead of guessing better words, this script reads them off the pages
(the technique is distant supervision: the known answer marks where the attribute is printed):

  for every validated answer of a number attribute (gold_attributes.json), the pages of the company's
  documents for the answer's year or the next that print the value (in any scale: value_eval.py's
  printed forms); on each, the words in the WINDOW characters before each printing of the value, as
  phrases of one to three words; each phrase counted once per answer.

A phrase is suggested when it is next to the value for at least MIN_ANSWERS answers of MIN_COMPANIES
companies and is much more frequent there than on an ordinary page (a random sample of BACKGROUND pages):
score = answers with it × log(lift). The report also gives the share of answers whose value has one of
the current labels nearby (how well the labels cover) and the document types where the value was found
(a suggested doc_types order). The same code for every attribute; no model.

Read-only: the gold bases (GOLD_TOKEN), the evidence database (SUPABASE_DB_URL). Writes label_mining.md
(also a run artifact) and the run's counters in Pipeline QA. Phrases are a few words long; no page text
is printed. Options: --limit N answers per attribute (dry run default 60), --attributes codes,
--budget minutes (100), --commit (Pipeline QA).
"""
import argparse, collections, json, math, os, re, sys, time, traceback

import qa
from value_eval import CURRENCY_SCALES, EMISSION_SCALES, printed_forms, tsquery_for

HERE = os.path.dirname(os.path.abspath(__file__))
WINDOW = 90                         # characters before the number where its label sits
MAX_PAGES = 20                      # pages per answer that print the value
MIN_ANSWERS, MIN_COMPANIES = 3, 3
BACKGROUND = 3000
TOP = 25
STOP = set("""a an and are as at be by for from has have in into is it its of on or our per than that the their
this to was were which with we us total year years ended end fy note notes million millions thousand thousands
billion bn mn m k eur usd gbp jpy inr cny chf sek nok dkk krw twd cad aud brl zar""".split())
WORD = re.compile(r"[^\W\d_]{2,}(?:[-'][^\W\d_]+)*")
NUM = re.compile(r"\d[\d,.'’    ]*")


def words(text):
    return [w.lower() for w in WORD.findall(text or "")]


def phrases(ws, n_max=3):
    """Phrases of one to n_max words, not starting or ending with a stop word."""
    out = set()
    for n in range(1, n_max + 1):
        for i in range(len(ws) - n + 1):
            p = ws[i:i + n]
            if p[0] in STOP or p[-1] in STOP:
                continue
            out.add(" ".join(p))
    return out


def number_spans(text, r, d):
    """Start offsets where the printed number r (with d decimals) appears in the text, in the separators
    reports use: 513,727 / 513.727 / 513 727 / 513'727 / 513727, and Indian grouping 5,13,727."""
    ip, _, dec = f"{r:.{d}f}".partition(".")
    groups = f"{int(ip):,}".split(",")
    alts = {ip}
    for sep in [",", ".", "'", "’", " ", " ", " "]:
        alts.add(sep.join(groups))
    if len(ip) > 3:                     # Indian: last three digits, then pairs
        head, tail = ip[:-3], ip[-3:]
        pairs = [head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1]
        alts.add(",".join(pairs + [tail]))
    pat = "(?:" + "|".join(re.escape(a) for a in sorted(alts, key=len, reverse=True)) + ")"
    if dec:
        pat = f"{pat}[.,]{re.escape(dec)}"
    rx = re.compile(rf"(?<![\d.,]){pat}(?![\d]|[.,]\d)")
    return [m.start() for m in rx.finditer(text or "")]


def label_window(text, start):
    """The words just before a number: up to WINDOW characters, cut at the previous line of figures."""
    seg = (text or "")[max(0, start - WINDOW):start]
    return words(seg)[-8:]


def mine_answer(pages, forms):
    """(phrases near the value on any page, doc types where it was found, windows) for one answer.
    pages: [(doc_type, body)]; forms: printed_forms of the value."""
    near, types, windows = set(), set(), []
    for doc_type, body in pages:
        hit = False
        for r, _, d in forms:
            for s in number_spans(body, r, d):
                w = label_window(body, s)
                if w:
                    near |= phrases(w); windows.append(w); hit = True
        if hit:
            types.add(doc_type or "(no type)")
    return near, types, windows


def suggest(counts, companies, bg, bg_n, answers, current, top=TOP):
    """[(phrase, answers with it, companies, lift, score, already a label)] best first."""
    out = []
    for p, k in counts.items():
        if k < MIN_ANSWERS or len(companies[p]) < MIN_COMPANIES:
            continue
        share = k / max(answers, 1)
        base = (bg.get(p, 0) + 1) / (bg_n + 1)
        lift = share / base
        if lift <= 2:
            continue
        out.append((p, k, len(companies[p]), lift, k * math.log(lift), p in current))
    out.sort(key=lambda x: -x[4])
    kept = []
    for x in out:                       # drop a phrase inside a better-ranked longer one with as many answers
        if any(x[0] in y[0] and y[0] != x[0] and y[1] >= x[1] for y in kept):
            continue
        kept.append(x)
        if len(kept) >= top:
            break
    return kept


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--attributes", default="")
    ap.add_argument("--budget", type=int, default=100)
    a = ap.parse_args()
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    limit = a.limit or (0 if a.commit else 60)
    token = os.environ.get("GOLD_TOKEN", "").strip()
    if not token:
        open("label_mining.md", "w").write("### Label mining: GOLD_TOKEN is not set; nothing run.\n")
        return 0
    import psycopg
    from value_eval import gold_values
    spec = json.load(open(os.path.join(HERE, "attributes.json"), encoding="utf-8"))["attributes"]
    gold = json.load(open(os.path.join(HERE, "gold_attributes.json"), encoding="utf-8"))
    codes = [c for c, v in gold["attributes"].items() if v.get("type") == "number"
             and (not a.attributes or c in a.attributes.split(","))]
    run = qa.Run("label_mining.py")
    answers = [g for base in gold["bases"].values() for g in gold_values(token, base, codes)
               if g["value"] and not g["unit"].lower().startswith(("%", "percent"))]
    by = collections.defaultdict(list)
    for g in answers:
        by[g["attribute"]].append(g)
    queues = [by[c][:limit] if limit else by[c] for c in codes]
    todo = [q[i] for i in range(max((len(q) for q in queues), default=0)) for q in queues if i < len(q)]
    print(f">>> {len(todo):,} answers ({', '.join(f'{c} {len(q)}' for c, q in zip(codes, queues))})", flush=True)

    counts = {c: collections.Counter() for c in codes}
    comps = {c: collections.defaultdict(set) for c in codes}
    types = {c: collections.Counter() for c in codes}
    st = {c: collections.Counter() for c in codes}
    t0, cut = time.time(), False
    with psycopg.connect(os.environ["SUPABASE_DB_URL"].strip(), connect_timeout=30) as conn:
        cur = conn.cursor()
        cur.execute("set statement_timeout = '60s'")
        cur.execute("select body from public.pages tablesample system (0.05) limit %s", (BACKGROUND,))
        bg, bg_n = collections.Counter(), 0
        for (body,) in cur.fetchall():
            bg.update(phrases(words(body))); bg_n += 1
        print(f">>> background: {bg_n:,} pages", flush=True)
        cur.execute("set statement_timeout = '30s'")
        for i, g in enumerate(todo, 1):
            if time.time() - t0 > a.budget * 60:
                cut = True; break
            if i % 100 == 0:
                print(f"  {i:,}/{len(todo):,} in {(time.time() - t0) / 60:.0f} min", flush=True)
            c = g["attribute"]; s = st[c]; s["answers"] += 1
            fam = (spec.get(c) or {}).get("unit_family")
            scales = EMISSION_SCALES if fam == "tco2e" else CURRENCY_SCALES if fam == "currency" else tuple(sorted(set(CURRENCY_SCALES) | set(EMISSION_SCALES)))
            forms = printed_forms(g["value"], scales)
            if not forms:
                continue
            q = " | ".join(f"({tsquery_for(v, d)})" for v, _, d in forms)
            try:
                cur.execute("select coalesce(d.doc_type, ''), p.body from public.pages p join public.documents d using (library_id) "
                            "where d.wba_id = %s and d.year in (%s, %s) and p.words @@ to_tsquery('simple', %s) limit %s",
                            (g["wba"], g["year"], g["year"] + 1, q, MAX_PAGES))
                pages = cur.fetchall()
            except Exception:
                conn.rollback(); cur.execute("set statement_timeout = '30s'")
                s["database did not answer"] += 1; continue
            near, tps, windows = mine_answer(pages, forms)
            if not near:
                continue
            s["value found"] += 1
            current = set(l.lower() for l in (spec.get(c) or {}).get("labels", []))
            if any(any(l in " ".join(w) for l in current) for w in windows):
                s["a current label nearby"] += 1
            counts[c].update(near)
            for p in near:
                comps[c][p].add(g["wba"])
            types[c].update(tps)

    md = ["### Labels learned from the validated answers" + ("" if a.commit else " (dry run, sample)"), "",
          f"{sum(s['answers'] for s in st.values()):,} answers" + (f" ({limit} per attribute)" if limit else "")
          + ("; stopped at the time budget" if cut else "") + f". Background: {bg_n:,} random pages.", "",
          "Phrases printed just before the validated value, in at least "
          f"{MIN_ANSWERS} answers of {MIN_COMPANIES} companies and far more often than on an ordinary page. "
          "✓ = already a label in attributes.json. Candidates for `labels` (or, when they mislead, `exclude`); nothing is changed automatically.", ""]
    for c in codes:
        s = st[c]; n = s["value found"] or 1
        name = (spec.get(c) or gold["attributes"][c]).get("name")
        md += [f"#### {c} {name}", "",
               f"Answers {s['answers']:,}; value printed on a page of the year's documents for {s['value found']:,}; "
               f"a current label within {WINDOW} characters before it for {s['a current label nearby']:,} ({s['a current label nearby'] / n:.0%}).", "",
               "Document types where the value was found: " + (", ".join(f"{t} {k:,}" for t, k in types[c].most_common(8)) or "none"), "",
               "| Phrase | Answers | Companies | Times more frequent than on an ordinary page | Label already |",
               "| --- | --: | --: | --: | :-: |"]
        cur_labels = set(l.lower() for l in (spec.get(c) or {}).get("labels", []))
        for p, k, nc, lift, _, has in suggest(counts[c], comps[c], bg, bg_n, s["value found"], cur_labels):
            md.append(f"| {p} | {k:,} | {nc:,} | {lift:,.0f} | {'✓' if has else ''} |")
        md.append("")
    open("label_mining.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    run.finish("ok", rows_in=len(todo), rows_out=sum(s["value found"] for s in st.values()),
               counters={c: dict(st[c]) for c in codes})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("label_mining.md", "w").write("### Label mining: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
