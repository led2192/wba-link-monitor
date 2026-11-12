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

    python fix_units.py [--commit] [--ambiguous]

--ambiguous (12 Nov 2026) is a second pass, for emissions rows whose unit is "MT", "mt" or "Mt" (with or without
CO2e) and where no scale was applied. That unit is metric tons in most American reports and megatonnes in others,
and the unit alone cannot say. Two things can, and each is recorded as the reason:
  the page: it is read from the evidence database, and when it spells out "metric tons" (and never "million
    tonnes" or "megatonnes") the unit is tonnes;
  the size: no company emits 2,000 megatonnes of Scope 1 or Scope 2 in a year (the largest emitters report some
    hundreds), and no Scope 3 figure is larger than the world's emissions (about 60,000 megatonnes), so a figure
    above that bound printed with "MT" is in metric tons. This is a bound on what is possible, not a guess from
    how big the number looks: below it nothing is decided.
A row the page says is megatonnes keeps its unit (the value is then in megatonnes, as printed). Whatever neither
the page nor the bound settles is left and listed. Needs SUPABASE_DB_URL for the pages.

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


AMBIGUOUS = re.compile(r"^mt(co2(e|eq|equiv|equivalents?)?)?$")
METRIC = re.compile(r"metric\s+tonn?e?s?\b", re.I)
MEGA = re.compile(r"millions?\s+(of\s+)?(metric\s+)?tonn?e?s?\b|mega\s?tonn?e?s?\b|\bMt\s*=", re.I)
BOUND = {"GMC_00005.1": 60000.0}            # megatonnes: more than the world emits in a year; Scope 3 can be large
BOUND_DEFAULT = 2000.0                      # megatonnes of Scope 1 or 2: several times the largest emitter on record


def is_ambiguous(unit):
    return bool(AMBIGUOUS.match(re.sub(r"[\s\-_.,/()]+", "", (unit or "").replace("₂", "2")).lower()))


def settle(attribute, value, body):
    """("tonnes" | "megatonnes" | None, reason) for one ambiguous row. body: the text of its page, or None."""
    metric, mega = bool(body and METRIC.search(body)), bool(body and MEGA.search(body))
    if metric and not mega:
        return "tonnes", "the page spells out metric tons"
    if mega and not metric:
        return "megatonnes", "the page spells out million tonnes"
    try:
        if abs(float(value)) > BOUND.get(attribute, BOUND_DEFAULT):
            return "tonnes", "more megatonnes than is possible"
    except (TypeError, ValueError):
        pass
    return None, "the page says both" if metric and mega else "neither the page nor the size settles it"


def plan_ambiguous(records, families, bodies):
    """(changes, counts, left [(value_id, unit, value, reason)]). bodies: {(library_id, page): text}."""
    c = collections.Counter()
    todo, left = [], []
    for rec in records:
        f = rec.get("fields", {})
        unit = (f.get("unit") or "").strip()
        if families.get(f.get("attribute")) != "tco2e" or (f.get("unit_printed") or "").strip() or not is_ambiguous(unit) \
                or scale_of(f.get("detail")) != 1:
            continue
        what, why = settle(f.get("attribute"), f.get("value"), bodies.get((f.get("library_id"), f.get("page"))))
        c[f"{what or 'left'}: {why}"] += 1
        if what == "tonnes":
            todo.append((rec["id"], f.get("value_id") or "", unit, "tCO2e"))
        elif what is None:
            left.append((f.get("value_id") or "", unit, f.get("value"), why))
    c["to correct"] = len(todo)
    return todo, c, left


def page_bodies(keys):
    """{(library_id, page): body} from the evidence database, 200 pages a question, by the pages' own key."""
    import load_supabase
    out, keys = {}, sorted(k for k in keys if k[0] and k[1])
    if not keys:
        return out
    conn = load_supabase.connect(os.environ["SUPABASE_DB_URL"])
    try:
        for i in range(0, len(keys), 200):
            part = keys[i:i + 200]
            with conn.cursor() as cur:
                cur.execute("select library_id, page, body from public.pages where library_id = any(%s) and page = any(%s)",
                            (sorted({k[0] for k in part}), sorted({int(k[1]) for k in part})))
                for lid, page, body in cur.fetchall():
                    if (lid, page) in set(part):
                        out[(lid, page)] = body
            conn.rollback()
    finally:
        conn.close()
    return out


def payloads(todo):
    for i in range(0, len(todo), WRITE_BATCH):
        yield {"records": [{"id": rid, "fields": {"unit": new, "unit_printed": old}} for rid, _, old, new in todo[i:i + WRITE_BATCH]]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--ambiguous", action="store_true", help="the second pass: MT with no scale applied, settled by the page or by what is possible")
    a = ap.parse_args()
    if a.ambiguous and not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL (the pages are read from the evidence database).")
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
    records = list(sweep("attribute_values", ["value_id", "attribute", "unit", "unit_printed", "detail", "value", "library_id", "page"],
                         "{method}='model reader'"))
    left = []
    if a.ambiguous:
        mine = [r for r in records if families.get(r.get("fields", {}).get("attribute")) == "tco2e" and is_ambiguous(r.get("fields", {}).get("unit"))]
        bodies = page_bodies({(r["fields"].get("library_id"), r["fields"].get("page")) for r in mine})
        print(f"{len(mine):,} emissions rows with an MT unit; {len(bodies):,} of their pages read", flush=True)
        todo, c, left = plan_ambiguous(mine, families, bodies)
    else:
        todo, c = plan(records, families)
    print(f"{len(records):,} model reader rows: " + ", ".join(f"{k} {v:,}" for k, v in sorted(c.items()) if v), flush=True)
    for (old, new), n in collections.Counter((o, n) for _, _, o, n in todo).most_common(40):
        print(f"  {n:>6,}  {old!r} > {new!r}", flush=True)
    for _, vid, old, new in todo[:20]:
        print(f"  e.g. {vid}: {old!r} > {new!r}", flush=True)
    for vid, unit, value, why in left[:60]:
        print(f"  left {vid}: {value} {unit!r} ({why})", flush=True)
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
