#!/usr/bin/env python3
"""
gold_inventory.py: which attributes have validated answers we could measure on, and of what kind?

Every reader experiment so far (Oct to Nov 2026) used 11 pilot attributes (gold_attributes.json): 6 numbers
and 3 narrative or option answers. The owner asked (7 Nov 2026) whether techniques that do nothing for these
might work for other kinds of attributes. This counts, in each gold base (read-only, GOLD_TOKEN), the reviewed
answers with a selected value per attribute code, and what the values look like: a number with a unit, a
percentage, yes or no, or text. Counts only: no value, no company, no answer is printed.

    python gold_inventory.py            (writes gold_inventory.md)
"""
import collections, json, os, re, sys, traceback

KINDS = ("number", "percent", "yes/no", "text")


def kind(value):
    v = (value or "").strip()
    if re.fullmatch(r"(?i)(yes|no|true|false|y|n)\.?", v):
        return "yes/no"
    if re.match(r"^-?\d+(\.\d+)?\s*(%|percent)", v, re.I):
        return "percent"
    if re.match(r"^-?\d+(\.\d+)?(\s+\S+)?\s*$", v):
        return "number"
    return "text"


def tally(rows, gold_id):
    """{code: Counter of kinds, plus 'answers' and 'companies'} from Metadata rows."""
    out, firms = collections.defaultdict(collections.Counter), collections.defaultdict(set)
    for r in rows:
        f = r.get("fields", {})
        wba, code, _year = gold_id(f.get("Metadata ID"))
        if not code:
            continue
        out[code]["answers"] += 1
        out[code][kind(f.get("Selected Value"))] += 1
        firms[code].add(wba)
    for c in out:
        out[c]["companies"] = len(firms[c])
    return out


def main():
    token = os.environ.get("GOLD_TOKEN", "").strip()
    if not token:
        open("gold_inventory.md", "w").write("### Gold inventory: GOLD_TOKEN is not set; nothing run.\n")
        return 0
    from gold_eval import gold_id
    from import_source_library import get_all
    cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "gold_attributes.json"), encoding="utf-8"))
    pilot = set(cfg["attributes"])
    md = ["### Validated answers per attribute, by kind of value", "",
          "Reviewed answers with a selected value in each gold base. Pilot: one of the 11 attributes every experiment used so far.", ""]
    for name, base in cfg["bases"].items():
        rows = get_all(token, base, "Metadata", ["Metadata ID", "Selected Value"], "AND({Data Reviewed}, {Selected Value}!='')")
        t = tally(rows, gold_id)
        md += [f"#### {name}: {sum(c['answers'] for c in t.values()):,} answers, {len(t):,} attributes", "",
               "| Attribute | Pilot | Answers | Companies | Number | Percent | Yes/no | Text |", "| --- | --- | --: | --: | --: | --: | --: | --: |"]
        for code, c in sorted(t.items(), key=lambda x: -x[1]["answers"]):
            md.append(f"| {code} | {'yes' if code in pilot else ''} | {c['answers']:,} | {c['companies']:,} | " + " | ".join(f"{c[k]:,}" for k in KINDS) + " |")
        md.append("")
    open("gold_inventory.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("gold_inventory.md", "w").write("### Gold inventory: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
