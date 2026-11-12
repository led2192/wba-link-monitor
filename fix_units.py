#!/usr/bin/env python3
"""
fix_units.py: the unit of the values already proposed, put right once.

Why (12 Nov 2026): `value` in attribute_values is the printed number times the page's scale, but `unit` was the
unit as printed. A page that prints 0.39 Mt-CO2e gave value 390,000 with unit "Mt-CO2e". New proposals are
written right since #458 and #459 (`model_reader.shown_unit`); this script corrects the rows written before.

What it does: for every row of method "model reader" with a unit, it reads the scale from the row's own `detail`
("Printed as 0.39 (millions) ..."), works out the unit the value is in (`shown_unit`: the scale taken out of
the unit; tonnes of CO2e written one way, tCO2e, for emissions attributes) and, where that differs from the unit
on the row, writes it to `unit` and moves the old one to `unit_printed`. The value is never touched. A row that
already has `unit_printed`, a row whose detail names no scale, and a unit that cannot be told (MT with no scale
applied) are left as they are.

    python fix_units.py [--commit]

Dry run prints the count of each change (old unit > new unit) and 20 examples. Env: AIRTABLE_TOKEN, AIRTABLE_BASE;
AIRTABLE_QA_TOKEN for the run record.
"""
import argparse, collections, json, os, re, sys, time

from model_reader import shown_unit

HERE = os.path.dirname(os.path.abspath(__file__))
SCALE_OF = {"units": 1, "thousands": 1e3, "ten thousands": 1e4, "lakhs": 1e5, "millions": 1e6, "crores": 1e7, "hundred millions": 1e8, "billions": 1e9}
DETAIL = re.compile(r"^Printed as [^()]*\(([a-z ]+)\)")
WRITE_BATCH = 10


def scale_of(detail):
    """The scale the row's own detail names, or None."""
    m = DETAIL.match(detail or "")
    return SCALE_OF.get(m.group(1)) if m else None


def plan(records, families):
    """(changes [(record id, value_id, old unit, new unit)], counts). families: attribute code -> unit family."""
    c = collections.Counter()
    todo = []
    for rec in records:
        f = rec.get("fields", {})
        unit = (f.get("unit") or "").strip()
        if not unit:
            c["no unit"] += 1
            continue
        if (f.get("unit_printed") or "").strip():
            c["already corrected"] += 1
            continue
        scale = scale_of(f.get("detail"))
        if scale is None:
            c["no scale in the detail"] += 1
            continue
        new = shown_unit(unit, scale, families.get(f.get("attribute")))
        if new == unit:
            c["right as it is, or cannot be told"] += 1
            continue
        todo.append((rec["id"], f.get("value_id") or "", unit, new))
    c["to correct"] = len(todo)
    return todo, c


def payloads(todo):
    for i in range(0, len(todo), WRITE_BATCH):
        yield {"records": [{"id": rid, "fields": {"unit": new, "unit_printed": old}} for rid, _, old, new in todo[i:i + WRITE_BATCH]]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    for v in ("AIRTABLE_TOKEN", "AIRTABLE_BASE"):
        if not os.environ.get(v):
            sys.exit(f"Set {v}.")
    import qa
    from base_health import sweep, API
    from monitor_core import airtable_request
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("fix_units.py")
    spec = json.load(open(os.path.join(HERE, "attributes.json"), encoding="utf-8"))["attributes"]
    families = {code: (at or {}).get("unit_family") for code, at in spec.items()}
    records = list(sweep("attribute_values", ["value_id", "attribute", "unit", "unit_printed", "detail"], "{method}='model reader'"))
    todo, c = plan(records, families)
    print(f"{len(records):,} model reader rows: " + ", ".join(f"{k} {v:,}" for k, v in sorted(c.items()) if v), flush=True)
    for (old, new), n in collections.Counter((o, n) for _, _, o, n in todo).most_common(40):
        print(f"  {n:>6,}  {old!r} > {new!r}", flush=True)
    for _, vid, old, new in todo[:20]:
        print(f"  e.g. {vid}: {old!r} > {new!r}", flush=True)
    written = 0
    if a.commit:
        headers = {"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}", "Content-Type": "application/json"}
        for body in payloads(todo):
            airtable_request("PATCH", f"{API}/{os.environ['AIRTABLE_BASE']}/attribute_values", headers, payload=body)   # two text fields: no typecast
            written += len(body["records"])
            time.sleep(0.22)
    print(f"{written:,} written" + ("" if a.commit else " (dry run: nothing written)"), flush=True)
    run.finish("ok", rows_in=len(records), rows_out=written, counters=dict(c), notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
