#!/usr/bin/env python3
"""
element_eval.py: what the analysts cited last year for each element, and how much of it the library holds.

The 2025 Social and non-Social benchmarks were verified at element level (an element is a question of a
benchmark's methodology, scored Met, Partially met or Unmet), not attribute by attribute: only the Climate
team (ACT Core) verified attributes. The Indicators bases keep, per company and element, the WBA score, the
analyst's evidence (literal quotes, each ending with the document type and page, "[Annual Report, p.92,93]")
and links to the cited sources. That is the labelled set for the narrative questions the ITA methodology will
ask the library: who answered them, from which kind of document, and whether our library could have.

Two stages, both read only:
  1. the analysts' evidence (Airtable only, every element row with evidence):
       by benchmark, by theme (the methodology's first category) and by WBA score: the kinds of source cited
       (Sources: Source Type, e.g. Sustainability Report, Annual Report, Policy Documents, Webpage), how many
       sources and quotes an element needed;
       by headquarters country (data/sdg2000_2025.csv): the kinds of source cited;
  2. the library (a sample, --sample rows spread over the benchmarks): for each cited source, whether the
     Disclosure Library holds it (same company and file size or specific file name, as sl_labels.match, or
     the same address), whether its text is in the evidence database, and whether the analyst's quote is in
     that text (a six-word phrase search, as gold_eval.py).

Reads the Indicators bases with GOLD_TOKEN (read only; ELEMENT_BASES in this file), the Disclosure Library
with AIRTABLE_TOKEN (read only) and the evidence database with SUPABASE_DB_URL (read only). Writes
element_eval.md. Never writes to any base. --commit only records the run in Pipeline QA.
"""
import argparse, collections, csv, os, re, statistics, sys, time, traceback

import qa

HERE = os.path.dirname(os.path.abspath(__file__))
# The Indicators bases of the 2025 benchmarks (memory of 2 Nov 2026: the non-Social base's Elements table holds
# every element of last year; the Social base is read too when the token reaches it).
ELEMENT_BASES = {"Indicators non-Social": "appwMpl1NQSIR4XXb", "Indicators Social": "appGm2woj8TR51VPT"}
E_TABLE, S_TABLE, M_TABLE = "Elements", "Sources", "Methodologies"
E_FIELDS = ["Assessment ID", "WBA Score", "Evidence", "Source Link", "Grouping Category 3"]
S_FIELDS = ["Source ID", "WBA ID", "Source Type", "Document Type", "Source Year", "Source Link", "Source File"]
M_FIELDS = ["Category 3 UIC", "Methodology Name", "Category 1 Name", "Category 2 Name", "GEC Short Description"]
TAG = re.compile(r"\[([^\[\]]{2,80}?),\s*p{1,2}\.?\s*([\d][\d,\s\-–]*)\]")
SEPARATOR = re.compile(r"\n?-{6,}\n?")


# ----------------------------------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------------------------------
def name_of(v):
    """A select's name, a lookup's first value, or the text itself."""
    if isinstance(v, dict):
        return v.get("name") or ""
    if isinstance(v, list):
        return name_of(v[0]) if v else ""
    return v or ""


def quotes(evidence):
    """[(quote text, cited type, [pages])]: the evidence split at the analysts' separators (a line of dashes);
    each part's closing tag "[Annual Report, p.92,93]" gives the type and pages, when it has one."""
    out = []
    for part in SEPARATOR.split(evidence or ""):
        part = part.strip()
        if not part:
            continue
        tags = TAG.findall(part)
        kind, pages = ("", [])
        if tags:
            kind = tags[-1][0].strip()
            pages = [int(p) for p in re.findall(r"\d{1,4}", tags[-1][1]) if 0 < int(p) < 5000]
        text = TAG.sub(" ", part).replace("[.]", " ")
        out.append((re.sub(r"\s+", " ", text).strip(), kind, pages))
    return out


def element_of(assessment_id):
    """'PT_00201-GEC_00527.2' -> ('PT_00201', 'GEC_00527.2')."""
    m = re.match(r"^(PT_\d{5})-(GEC_\d{5}\.\d+)$", (assessment_id or "").strip())
    return (m.group(1), m.group(2)) if m else (None, None)


def norm_url(u):
    u = (u or "").strip().lower().split("#")[0].split("?")[0]
    u = re.sub(r"^https?://(www\.)?", "", u)
    return u.rstrip("/")


def shares(counter, top=6):
    """'Sustainability Report 41%, Annual Report 22%, ...' for the top kinds of a Counter."""
    n = sum(counter.values()) or 1
    return ", ".join(f"{k or '(none)'} {v / n:.0%}" for k, v in counter.most_common(top))


SHARE_DIMS = [("all", lambda r: "all"), ("benchmark", lambda r: r.get("benchmark")), ("theme", lambda r: r.get("theme")),
              ("country", lambda r: r.get("country")), ("element", lambda r: r.get("gec")),
              ("benchmark x country", lambda r: f"{r.get('benchmark')} | {r.get('country')}")]


def share_rows(rows, min_n=20):
    """One row per (dimension, group, source type): how often that type was cited, over all element rows and over
    the Met ones only (a Met element cites where the evidence was found; an Unmet one often cites what was checked).
    3 Nov 2026: shared with the Ode team to weight sources per question, so it is a plain table, not prose."""
    out = []
    for dim, key in SHARE_DIMS:
        groups = collections.defaultdict(list)
        for r in rows:
            groups[key(r) or "(none)"].append(r)
        for g, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            if len(rs) < min_n:
                continue
            met = [r for r in rs if r["score"] == "Met"]
            allc = collections.Counter(t for r in rs for t in r["source_types"])
            metc = collections.Counter(t for r in met for t in r["source_types"])
            na, nm = sum(allc.values()) or 1, sum(metc.values()) or 1
            for t, v in allc.most_common():
                out.append({"dimension": dim, "group": g, "elements": len(rs), "elements_met": len(met),
                            "source_type": t or "(none)", "citations": v, "share": round(v / na, 4),
                            "citations_met": metc.get(t, 0), "share_met": round(metc.get(t, 0) / nm, 4) if met else ""})
    return out


def write_shares(rows, path="element_source_shares.csv"):
    out = share_rows(rows)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0]) if out else ["dimension"])
        w.writeheader()
        w.writerows(out)
    return len(out)


def record_scores(run, c):
    """The library stage as four scoreboard rows (Pipeline QA evals), over the whole sample, with each benchmark
    in per_class. Discovery (held), capture (text in the database) and extraction (a quote found)."""
    tot = collections.Counter()
    for s in c.values():
        tot.update(s)
    d, n = tot["document sources"], tot["elements"]
    if not n:
        return
    per = lambda k, base: {bm: {"n": s[base], "share": round(s[k] / (s[base] or 1), 4)} for bm, s in c.items()}
    for target, k, base, note in [
            ("cited documents held (same file)", "in library", "document sources",
             "Share of the documents the analysts cited in 2025 (element level) that the library holds as the same file."),
            ("cited documents held (same company, type and year)", "same type and year held", "document sources",
             "Share of the cited documents for which the library holds a document of the same company, type and year."),
            ("cited documents with text in the database", "text in database", "document sources",
             "Share of the cited documents held whose text is in the evidence database."),
            ("elements with a quote found", "a quote found in our text", "elements",
             "Share of sampled elements where one of the analyst's quotes is found in our text.")]:
        nb = tot[base]
        if nb:
            run.evaluation(target, nb, tot[k] / nb, version="element_eval.py", per_class=per(k, base), notes=note,
                           suite="analyst citations 2025")


def summarize(rows, key):
    """{group: Counter} over element rows (dicts from read_elements), grouped by row[key]."""
    out = collections.defaultdict(collections.Counter)
    for r in rows:
        g = r.get(key) or "(none)"
        s = out[g]
        s["elements"] += 1
        s["score " + (r["score"] or "(none)")] += 1
        s["quotes"] += len(r["quotes"])
        s["sources"] += len(r["sources"])
        for t in r["source_types"]:
            s["type " + t] += 1
    return out


KIND_FAMILY = {"Sustainability Report": "SUST", "ESG Report": "SUST", "CSR Report": "SUST", "Impact Report": "SUST",
               "Integrated Report": "ANN", "Annual Report": "ANN", "10K Form": "ANN", "Registration Document": "ANN"}


def same_kind_held(src, wba, typed):
    """True when the library holds a document of the same company, the same kind (reports by family, others by
    type) and the same year or the next (a reissued file, a copy from another address): the looser match, an
    upper bound of what the library holds."""
    t = src.get("type") or ""
    fam = KIND_FAMILY.get(t, t)
    try:
        y = int(str(src.get("year") or "0")[:4])
    except ValueError:
        y = 0
    held = typed.get((wba, fam), set())
    return bool(held) and (not y or bool(held & {y, y + 1}) or t not in KIND_FAMILY and y == 0)


def library_types(token, base, wbas):
    """{(wba, family or type): {years}} of the library's documents (not discarded) of the given companies."""
    from monitor_core import airtable_request
    H = {"Authorization": f"Bearer {token}"}
    out, wbas = collections.defaultdict(set), sorted(set(w for w in wbas if w))
    for i in range(0, len(wbas), 40):
        formula = "AND(NOT({discard}), OR(" + ",".join(f"{{wba_id}}='{w}'" for w in wbas[i:i + 40]) + "))"
        params = [("pageSize", "100"), ("filterByFormula", formula)] + [("fields[]", x) for x in ("wba_id", "year", "source_type_check", "source_type")]
        offset = None
        while True:
            j = airtable_request("GET", f"https://api.airtable.com/v0/{base}/report_library", H,
                                 params=params + ([("offset", offset)] if offset else [])).json()
            for r in j.get("records", []):
                f = r["fields"]
                t = name_of(f.get("source_type_check")) or name_of(f.get("source_type"))
                try:
                    y = int(float(f.get("year")))
                except (TypeError, ValueError):
                    y = 0
                out[((f.get("wba_id") or "").strip(), KIND_FAMILY.get(t, t))].add(y)
            offset = j.get("offset")
            if not offset:
                break
            time.sleep(0.2)
    return out


# ----------------------------------------------------------------------------------------------
# Airtable (read only)
# ----------------------------------------------------------------------------------------------
def read_base(token, base):
    """(element rows, {source record id: source}) of one Indicators base."""
    from import_source_library import get_all
    meth = {}
    for r in get_all(token, base, M_TABLE, M_FIELDS):
        f = r["fields"]
        meth[r["id"]] = {"benchmark": name_of(f.get("Methodology Name")), "theme": f.get("Category 1 Name") or "",
                         "indicator": f.get("Category 2 Name") or "", "element": f.get("GEC Short Description") or ""}
    srcs = {}
    for r in get_all(token, base, S_TABLE, S_FIELDS):
        f = r["fields"]
        att = (f.get("Source File") or [{}])[0]
        srcs[r["id"]] = {"wba": (f.get("WBA ID") or "").strip(), "type": name_of(f.get("Source Type")),
                         "kind": name_of(f.get("Document Type")), "year": name_of(f.get("Source Year")),
                         "url": f.get("Source Link") or "", "name": f.get("Source ID") or "",
                         "size": att.get("size"), "filename": att.get("filename") or ""}
    rows = []
    for r in get_all(token, base, E_TABLE, E_FIELDS, "{Evidence}!=''"):
        f = r["fields"]
        wba, gec = element_of(f.get("Assessment ID"))
        if not wba:
            continue
        m = meth.get((f.get("Grouping Category 3") or [None])[0], {})
        sl = [srcs[s] for s in (f.get("Source Link") or []) if s in srcs]
        rows.append({"wba": wba, "gec": gec, "score": name_of(f.get("WBA Score")), "quotes": quotes(f.get("Evidence")),
                     "sources": sl, "source_types": [s["type"] or s["kind"] for s in sl],
                     "benchmark": m.get("benchmark", ""), "theme": m.get("theme", ""), "indicator": m.get("indicator", "")})
    return rows, srcs


# ----------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--sample", type=int, default=600, help="element rows checked against the library and the evidence database")
    ap.add_argument("--budget", type=int, default=90, help="minutes for the library stage")
    a = ap.parse_args()
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    token = os.environ.get("GOLD_TOKEN", "").strip()
    if not token:
        open("element_eval.md", "w").write("### Element evaluation: GOLD_TOKEN is not set; nothing run.\n")
        return 0
    run = qa.Run("element_eval.py")
    rows, notes = [], []
    for name, base in ELEMENT_BASES.items():
        try:
            got, _ = read_base(token, base)
        except Exception as e:                       # a base the token does not reach is reported, not fatal
            notes.append(f"{name} ({base}): not read ({type(e).__name__}: {str(e)[:120]})")
            continue
        for r in got:
            r["base"] = name
        rows += got
        notes.append(f"{name} ({base}): {len(got):,} element rows with evidence")
        print(f">>> {notes[-1]}", flush=True)
    iso = {}
    try:
        with open(os.path.join(HERE, "data", "sdg2000_2025.csv"), encoding="utf-8-sig", newline="") as fh:
            iso = {r["WBA ID"]: r["Location HQ ISO Code"] for r in csv.DictReader(fh)}
    except OSError:
        pass
    for r in rows:
        r["country"] = iso.get(r["wba"], "")
        r["tags"] = [k for _, k, _ in r["quotes"] if k]

    md = ["### Element evaluation: what the analysts cited in 2025, and what the library holds" + ("" if a.commit else " (dry run)"), ""]
    md += [f"- {n}" for n in notes] + [""]
    if not rows:
        open("element_eval.md", "w").write("\n".join(md) + "\nNo element rows read.\n")
        run.finish("partial", rows_in=0, rows_out=0, counters={})
        return 0

    tot = collections.Counter(t for r in rows for t in r["source_types"])
    tags = collections.Counter(t for r in rows for t in r["tags"])
    md += [f"{len(rows):,} element rows with evidence, {len({r['wba'] for r in rows}):,} companies, "
           f"{len({r['gec'] for r in rows}):,} elements. Sources cited per element: median "
           f"{statistics.median([len(r['sources']) for r in rows]):.0f}; quotes per element: median "
           f"{statistics.median([len(r['quotes']) for r in rows]):.0f}.", "",
           f"Kinds of source cited (Sources: Source Type), all elements: {shares(tot, 10)}.", "",
           f"WBA scores: {shares(collections.Counter(r['score'] for r in rows), 8)}.", "",
           f"Document types named in the quotes' tags: {shares(tags, 10)}.", ""]

    def table(key, title, top=None, min_n=30):
        groups = summarize(rows, key)
        md.extend([f"#### By {title}", "", f"| {title.capitalize()} | Elements | Met | Unmet | Other scores | Sources per element | Kinds of source cited |",
                   "| --- | --: | --: | --: | --: | --: | --- |"])
        items = sorted(groups.items(), key=lambda kv: -kv[1]["elements"])
        for g, s in items[:top] if top else items:
            n = s["elements"]
            if n < min_n:
                continue
            types = collections.Counter({k[5:]: v for k, v in s.items() if k.startswith("type ")})
            other = n - s["score Met"] - s["score Unmet"]
            md.append(f"| {g} | {n:,} | {s['score Met'] / n:.0%} | {s['score Unmet'] / n:.0%} | {other / n:.0%} | "
                      f"{s['sources'] / n:.1f} | {shares(types, 5)} |")
        md.append("")

    n_shares = write_shares(rows)
    print(f">>> element_source_shares.csv: {n_shares:,} rows (dimension, group, source type)", flush=True)
    table("benchmark", "benchmark")
    table("theme", "theme", top=25)
    table("score", "WBA score", min_n=1)
    table("country", "headquarters country", top=15)

    # ---- stage 2: the library ----------------------------------------------------------------------------
    import random
    from gold_eval import library_rows, quote_phrases, pages_with_phrase, doc_in_db
    from sl_labels import match
    rnd = random.Random(20261102)
    by_bm = collections.defaultdict(list)
    for r in rows:
        if r["sources"]:
            by_bm[r["benchmark"]].append(r)
    sample = []
    for bm, rs in sorted(by_bm.items()):
        rnd.shuffle(rs)
    while len(sample) < a.sample and any(by_bm.values()):
        for bm in sorted(by_bm):
            if by_bm[bm] and len(sample) < a.sample:
                sample.append(by_bm[bm].pop())
    lib = library_rows(os.environ["AIRTABLE_TOKEN"], os.environ["AIRTABLE_BASE"], [r["wba"] for r in sample])
    typed = library_types(os.environ["AIRTABLE_TOKEN"], os.environ["AIRTABLE_BASE"], [r["wba"] for r in sample])
    c = collections.defaultdict(collections.Counter)
    t0, cut = time.time(), False
    import psycopg
    with __import__("evidence_db").connect() as conn:
        cur = conn.cursor()
        cur.execute("set statement_timeout = '60s'")
        for i, r in enumerate(sample, 1):
            if time.time() - t0 > a.budget * 60:
                cut = True; break
            s = c[r["benchmark"] or "(none)"]
            s["elements"] += 1
            docs = [x for x in r["sources"] if (x["kind"] or "").lower() != "webpage" and (x["type"] or "").lower() != "webpage"]
            s["sources"] += len(r["sources"]); s["webpage sources"] += len(r["sources"]) - len(docs)
            pairs = match([dict(d, wba=r["wba"]) for d in docs], [x for x in lib if x.get("wba_id") == r["wba"]])
            s["document sources"] += len(docs); s["in library"] += len(pairs)
            s["same type and year held"] += sum(1 for d in docs if same_kind_held(d, r["wba"], typed))
            held = [p[1]["library_id"] for p in pairs if p[1].get("library_id")]
            in_db = [lid for lid in held if doc_in_db(cur, lid)]
            s["text in database"] += len(in_db)
            s["every document source held"] += bool(docs) and len(pairs) == len(docs)
            found = False
            for q, _, _ in r["quotes"]:
                for ph in quote_phrases(q)[:2]:
                    for lid in in_db:
                        try:
                            if pages_with_phrase(cur, lid, ph):
                                found = True; break
                        except Exception:
                            conn.rollback(); cur.execute("set statement_timeout = '60s'")
                    if found:
                        break
                if found:
                    break
            s["a quote found in our text"] += found
    md += ["#### The library against the cited sources (sample" + (", cut by the time budget" if cut else "") + ")", "",
           "Element rows spread over the benchmarks. A document source is held when the library has the same company's file of the "
           "same size or specific name; webpage sources are counted apart (the library holds pages, not their text).", "",
           "| Benchmark | Elements | Sources | Webpages | Documents held (same file) | Same company, type and year held | Text in database | Elements with every document held | A quote found in our text |",
           "| --- | --: | --: | --: | --: | --: | --: | --: | --: |"]
    for bm, s in sorted(c.items(), key=lambda kv: -kv[1]["elements"]):
        n, d = s["elements"] or 1, s["document sources"] or 1
        md.append(f"| {bm} | {s['elements']:,} | {s['sources']:,} | {s['webpage sources'] / (s['sources'] or 1):.0%} | "
                  f"{s['in library'] / d:.0%} | {s['same type and year held'] / d:.0%} | {s['text in database'] / d:.0%} | {s['every document source held'] / n:.0%} | "
                  f"{s['a quote found in our text'] / n:.0%} |")
    md.append("")
    text = "\n".join(md) + "\n"
    open("element_eval.md", "w").write(text)
    print(text, flush=True)
    record_scores(run, c)
    run.finish("ok", rows_in=len(rows), rows_out=sum(s["elements"] for s in c.values()),
               counters={"element rows": len(rows), "sampled": sum(s["elements"] for s in c.values())})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("element_eval.md", "w").write("### Element evaluation: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
