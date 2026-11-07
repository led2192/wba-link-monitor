#!/usr/bin/env python3
"""
visual_eval.py: are the figures we cannot find in the text printed in pictures?

For 9% to 23% of validated answers the analyst's figure is not in the text of the company's documents for the
year (extract_eval, 6 Nov 2026), although the documents are there. One reading (research shared by the owner,
7 Nov 2026): reports print key figures in charts, infographics and tables saved as images, which text
extraction loses. Before building any visual route, this measures how often that is the reason:

  1. the misses: validated answers (number attributes, percentages apart) with documents for the year whose
     text holds no printing of the analyst's figure (the same test as extract_eval's "number in those
     documents");
  2. for each, the PAGES best-ranked pages (extract_eval's ranking) are rendered as images from their PDF and
     shown to the model, which is asked for the attribute's figure for the year as printed, from text,
     tables, charts or pictures;
  3. a figure that equals the analyst's at some scale is a figure the text lost: the share of misses the
     pictures would recover. A control set of answers whose figure IS in the text (CONTROL of them) measures
     the visual reader's own recall on the same kind of pages, so a low share is not mistaken for absence.

Counts and shares only. Read-only. Billed (ANTHROPIC_API_KEY, READER_MODEL; --max-usd caps it).
Options: --limit N answers scanned per attribute (200), --pages (4), --control (30), --max-usd (3), --budget.
"""
import argparse, base64, collections, json, os, re, sys, time, traceback

import qa
import extract_eval as E
from value_eval import found_on, printed_forms, scales_for, tsquery_for

PAGES = 4
DPI = 110
SYSTEM = ("You read pages of a company's report, given as images, and find one figure. Read tables, charts, "
          "infographics and pictures as well as text. Reply with one JSON object only.")


def render(path, page):
    """A JPEG of one PDF page, base64, or None."""
    try:
        import pymupdf
        doc = pymupdf.open(path)
        pix = doc[int(page) - 1].get_pixmap(dpi=DPI)
        data = pix.tobytes("jpeg")
        doc.close()
        return base64.b64encode(data).decode()
    except Exception:
        return None


def ask(attr, year, images, usage):
    """The model's (value, scale word, read from a picture) for the figure, or (None, None, None)."""
    from model_reader import call, parse_reply
    content = []
    for i, b64 in enumerate(images, 1):
        content += [{"type": "text", "text": f"Page {i}:"}, {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}]
    content.append({"type": "text", "text":
                    f"Find the company's {attr.get('name')} for the year {year} on these pages "
                    f"({'; '.join(attr.get('labels', [])[:4])}). Reply: {{\"value\": the number exactly as printed or null, "
                    "\"scale\": \"units\", \"thousands\", \"millions\" or \"billions\" as the page states, "
                    "\"page\": the page number, \"in_a_picture\": true if read from a chart, infographic or image, else false}"})
    raw = call([{"role": "user", "content": content}], usage, system=SYSTEM, max_tokens=300)
    rep = parse_reply(raw)
    if not isinstance(rep, dict) or rep.get("value") in (None, "", "null"):
        return None, None, None
    try:
        v = E.to_number(str(rep["value"]).replace(" ", ""))
    except (ValueError, TypeError):
        return None, None, None
    return v, str(rep.get("scale") or "units").lower(), bool(rep.get("in_a_picture"))


SCALE = {"units": 1, "thousands": 1e3, "millions": 1e6, "billions": 1e9}


def matches(v, scale_word, gold, scales):
    """"stated": the read figure equals the analyst's at the scale the model says the page states; "digits": only
    at another scale of the attribute's family (the stricter count is the first; a coincidence is likelier in
    the second); None: no match."""
    if v is None:
        return None
    if E.right(v * SCALE.get(scale_word, 1), gold):
        return "stated"
    return "digits" if any(E.right(v * s, gold) for s in scales) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--pages", type=int, default=PAGES)
    ap.add_argument("--control", type=int, default=30)
    ap.add_argument("--max-usd", type=float, default=3.0)
    ap.add_argument("--budget", type=int, default=150)
    a = ap.parse_args()
    token = os.environ.get("GOLD_TOKEN", "").strip()
    if not token or not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        open("visual_eval.md", "w").write("### Visual check: GOLD_TOKEN and ANTHROPIC_API_KEY are needed; nothing run.\n")
        return 0
    import table_text
    from model_reader import cost
    from value_eval import gold_values
    spec = json.load(open(E.SPEC, encoding="utf-8"))
    gold_cfg = json.load(open(os.path.join(os.path.dirname(E.SPEC), "gold_attributes.json"), encoding="utf-8"))
    codes = [c for c, v in spec["attributes"].items() if v["type"] == "number"]
    run = qa.Run("visual_eval.py")
    answers = [g for base in gold_cfg["bases"].values() for g in gold_values(token, base, codes)
               if g["value"] and not g["unit"].lower().startswith(("%", "percent"))]
    by = collections.defaultdict(list)
    for g in answers:
        by[g["attribute"]].append(g)
    queues = [by[c][:a.limit] for c in codes]
    todo = [q[i] for i in range(max((len(q) for q in queues), default=0)) for q in queues if i < len(q)]
    st = {c: collections.Counter() for c in codes}
    usage, t0, stop = collections.Counter(), time.time(), ""
    controls = 0
    with __import__("evidence_db").connect() as conn:
        cur = conn.cursor()
        cur.execute("set statement_timeout = '30s'")
        for g in todo:
            if time.time() - t0 > a.budget * 60:
                stop = "the time budget"; break
            if cost(usage) >= a.max_usd:
                stop = f"the ${a.max_usd:g} cost limit"; break
            attr = spec["attributes"][g["attribute"]]; s = st[g["attribute"]]
            try:
                docs, weight = E.year_documents(cur, g["wba"], g["year"], attr)
                if not docs:
                    continue
                scales = scales_for(attr.get("unit_family"))
                forms = printed_forms(g["value"], scales)
                ids = [d[0] for d in docs]
                q = " | ".join(f"({tsquery_for(v, d)})" for v, _, d in forms)
                cur.execute("select library_id, page, body from public.pages where library_id = any(%s) and words @@ to_tsquery('simple', %s) limit 40", (ids, q))
                in_text = any(any(found_on(b, v) for v, _, _ in forms) for _, _, b in cur.fetchall())
            except Exception:
                conn.rollback(); cur.execute("set statement_timeout = '30s'"); continue
            s["scanned"] += 1
            if in_text:
                s["in the text"] += 1
                if controls >= a.control:
                    continue
            group = "control" if in_text else "miss"
            try:
                pages = E.keep_pages(E.ranked_pages(cur, ids, weight, g["year"], attr, spec, {}, None, None))[:a.pages]
            except Exception:
                conn.rollback(); cur.execute("set statement_timeout = '30s'")
                s[group + " not ranked"] += 1; continue
            imgs = []
            for _, lid, page, _b, _t in pages:
                p = table_text.pdf_path(lid)
                b64 = render(p, page) if p else None
                if b64:
                    imgs.append(b64)
            if not imgs:
                s[group + " without images"] += 1; continue
            if group == "control":
                controls += 1
            s[group + " read"] += 1
            v, sw, pic = ask(attr, g["year"], imgs, usage)
            m = matches(v, sw, g["value"], scales)
            s[group + " found"] += m == "stated"
            s[group + " digits only"] += m == "digits"
            s[group + " found in a picture"] += bool(m == "stated" and pic)
            for pth in {table_text.pdf_path(lid) for _, lid, *_ in pages}:
                try:
                    if pth:
                        os.remove(pth)
                except OSError:
                    pass
    md = ["### Are the figures missing from the text printed in pictures?", "",
          f"Misses: validated answers with documents for the year whose text holds no printing of the analyst's figure. The {a.pages} "
          f"best-ranked pages were shown to {os.environ.get('READER_MODEL', 'claude-haiku-5-5')} as images. Control: answers whose figure "
          "is in the text, read the same way, to measure the visual reader itself. Counts and shares only."
          + (f" Stopped at {stop}." if stop else ""), "",
          "Found: the figure read equals the analyst's at the scale the page states. Digits only: equal only at another scale "
          "(likelier to be a coincidence, not counted as found). A miss whose figure the analyst calculated (a sum, a conversion) "
          "cannot be found on any page, so the share found is a lower bound of what pictures hide.", "",
          "| Attribute | Scanned | Figure in the text | Misses read as images | Found in the images | Of which in a picture | Digits only | Control read | Control found |",
          "| --- | --: | --: | --: | --: | --: | --: | --: | --: |"]
    for c in codes:
        s = st[c]
        if not s["scanned"]:
            continue
        pct = lambda x, n: f"{x / n:.0%}" if n else "-"
        md.append(f"| {c} {spec['attributes'][c].get('name', '')} | {s['scanned']:,} | {pct(s['in the text'], s['scanned'])} | {s['miss read']:,} | "
                  f"{s['miss found']:,} ({pct(s['miss found'], s['miss read'])}) | {s['miss found in a picture']:,} | {s['miss digits only']:,} | {s['control read']:,} | "
                  f"{pct(s['control found'], s['control read'])} |")
    md += ["", f"Model calls: {usage.get('calls', 0)}; cost about ${cost(usage):.2f}."]
    open("visual_eval.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    run.finish("ok", rows_in=len(todo), rows_out=sum(s["miss read"] for s in st.values()))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("visual_eval.md", "w").write("### Visual check: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
