#!/usr/bin/env python3
"""
gold_store.py: lanterN's own copy of WBA's validated answers, and the reader that serves it.

Why (14 Nov 2026, the owner's decision): lanterN is to stop reading WBA's internal Airtable bases. The validated
answers (the "gold" bases) are complete as they are and no new ones will come, so one dated copy is enough. 23
scripts read them, every one through import_source_library.get_all; the nightly reader reads them every night.

What is copied: from each gold base (the three of gold_attributes.json and the monthly Metadata 2025 bases of
narrative_eval.py), three tables with the fields the scripts ask for:
  Metadata        the answers a person at WBA reviewed, or the research provider marked complete
  Sources         the documents the analysts cited (their addresses; not the attached files, whose links expire)
  Metadata Codes  the attribute definitions
into public.gold_records of the search database (one row a record: base, table, record id, fields as JSON). The
database is private; this script prints counts only.

How it is used: import_source_library.get_all asks serves(base, table) first. When the copy holds that table of that
base, the rows come from the copy, filtered by the same formula text the scripts send to Airtable (a small reader of
the few formula shapes they use: AND, OR, NOT, {Field}, {Field}!='', {Field}='x', FIND('x', {Field})). The copy is
used only with GOLD_COPY=on until --verify has shown it gives the same records as Airtable.

    python gold_store.py --take [--commit]     copy the bases (dry run: counts only)
    python gold_store.py --verify              the same questions asked of Airtable and of the copy: the ids must match

Env: GOLD_TOKEN (read-only, for --take and --verify), SUPABASE_DB_URL; AIRTABLE_QA_TOKEN for the run record.
"""
import argparse, json, os, re, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
TABLES = {
    "Metadata": (["Metadata ID", "Selected Value", "Evidence", "Page Number", "Source Link", "Editor Interface Full URL",
                  "Null Value Reasoning", "GMC Type", "Data Reviewed", "RP Metadata Status"],
                 "OR({Data Reviewed}, {RP Metadata Status}='QC Complete')"),
    "Sources": (["Source ID", "Source Link", "Source Year", "WBA ID"], None),
    "Metadata Codes": (["Global Metadata Code (GMC)", "Description", "Definition", "Data Type", "Select Options", "Units"], None),
}
TABLE_SQL = """
create table if not exists public.gold_records (
  base text not null, tbl text not null, record_id text not null, fields jsonb not null,
  taken timestamptz not null default now(), primary key (base, tbl, record_id));
alter table public.gold_records enable row level security"""


def gold_bases():
    """{name: base id} of every gold base a script reads."""
    out = dict(json.load(open(os.path.join(HERE, "gold_attributes.json"), encoding="utf-8"))["bases"])
    try:
        from narrative_eval import MONTHLY_BASES
        for k, v in MONTHLY_BASES.items():
            out.setdefault(k, v)
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------------------------- the formula reader
def text_of(v):
    """A cell as Airtable's formulas see it in text: '' for empty, names joined for lists and selects."""
    if v is None or v is False:
        return ""
    if v is True:
        return "1"
    if isinstance(v, dict):
        return str(v.get("name") or v.get("text") or "")
    if isinstance(v, (list, tuple)):
        return ", ".join(text_of(x) for x in v)
    return str(v)


TOKEN = re.compile(r"\s*(AND\(|OR\(|NOT\(|FIND\(|\{[^}]*\}|'(?:[^']*)'|!=|=|&|,|\))")


def parse(formula):
    """The formula as a nested list. Raises ValueError on a shape this reader does not know, so that an unknown
    formula is never answered wrongly from the copy (the caller then goes to Airtable, or fails loudly)."""
    toks, pos = [], 0
    while pos < len(formula):
        m = TOKEN.match(formula, pos)
        if not m:
            if formula[pos:].strip() == "":
                break
            raise ValueError(f"formula not understood at {pos}")
        toks.append(m.group(1)); pos = m.end()
    i = 0

    def term():
        nonlocal i
        t = toks[i]
        if t in ("AND(", "OR(", "NOT("):
            i += 1
            args = [expr()]
            while toks[i] == ",":
                i += 1; args.append(expr())
            if toks[i] != ")":
                raise ValueError("no closing bracket")
            i += 1
            return [t[:-1]] + args
        if t == "FIND(":
            i += 1
            needle = value()
            if toks[i] != ",":
                raise ValueError("FIND takes two things")
            i += 1
            hay = value()
            if toks[i] != ")":
                raise ValueError("no closing bracket")
            i += 1
            return ["FIND", needle, hay]
        return value()

    def value():
        nonlocal i
        t = toks[i]
        if t.startswith("{"):
            node = ["FIELD", t[1:-1]]
        elif t.startswith("'"):
            node = ["TEXT", t[1:-1]]
        else:
            raise ValueError(f"unexpected {t}")
        i += 1
        while i < len(toks) and toks[i] == "&":          # {Field}&'' : the field as text
            i += 1
            node = ["CAT", node, value_atom()]
        return node

    def value_atom():
        nonlocal i
        t = toks[i]
        if t.startswith("{"):
            node = ["FIELD", t[1:-1]]
        elif t.startswith("'"):
            node = ["TEXT", t[1:-1]]
        else:
            raise ValueError(f"unexpected {t}")
        i += 1
        return node

    def expr():
        nonlocal i
        left = term()
        if i < len(toks) and toks[i] in ("!=", "="):
            op = toks[i]; i += 1
            return [op, left, value()]
        return left

    tree = expr()
    if i != len(toks):
        raise ValueError("formula has more than one expression")
    return tree


def holds(node, fields):
    """Whether a record's fields satisfy the formula (as Airtable's filterByFormula keeps a record)."""
    def val(n):
        if n[0] == "FIELD":
            return text_of(fields.get(n[1]))
        if n[0] == "TEXT":
            return n[1]
        if n[0] == "CAT":
            return val(n[1]) + val(n[2])
        raise ValueError("not a value")
    k = node[0]
    if k == "AND":
        return all(holds(a, fields) for a in node[1:])
    if k == "OR":
        return any(holds(a, fields) for a in node[1:])
    if k == "NOT":
        return not holds(node[1], fields)
    if k == "FIND":
        return val(node[1]) in val(node[2])
    if k == "!=":
        return val(node[1]) != val(node[2])
    if k == "=":
        return val(node[1]) == val(node[2])
    return val(node) != ""                                # a bare field: ticked or not empty


# ---------------------------------------------------------------------------------------------- serving the copy
_HELD = None               # {(base, table)} the copy holds, read once a run
_ROWS = {}                 # (base, table) -> [(record id, fields)], read once a run


def _connect():
    import evidence_db
    return evidence_db.connect(wait_minutes=2)


def serves(base, table, force=False):
    """True when this table of this base is answered from the copy: one of the three tables, the copy holds it, and
    GOLD_COPY is on (the copy runs beside Airtable until --verify has shown the same records; then on becomes the
    default). Any trouble reading the database means "no": the caller goes to Airtable."""
    global _HELD
    on = os.environ.get("GOLD_COPY", "").strip().lower() in ("on", "1", "true")
    if not (on or force) or table not in TABLES or not os.environ.get("SUPABASE_DB_URL", "").strip():
        return False
    if _HELD is None:
        try:
            with _connect() as conn:
                cur = conn.cursor()
                cur.execute("select to_regclass('public.gold_records') is not null")
                if not cur.fetchone()[0]:
                    _HELD = set()
                else:
                    cur.execute("select distinct base, tbl from public.gold_records")
                    _HELD = {(b, t) for b, t in cur.fetchall()}
        except Exception:
            _HELD = set()
    return (base, table) in _HELD


def serve(base, table, fields, formula=None):
    """The copy's records of a table, in the shape Airtable gives: [{"id", "fields": {asked fields that are not empty}}]."""
    if (base, table) not in _ROWS:
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute("select record_id, fields from public.gold_records where base = %s and tbl = %s order by record_id", (base, table))
            _ROWS[(base, table)] = cur.fetchall()
    tree = parse(formula) if formula else None
    want = set(fields or [])
    unknown = want - set(TABLES[table][0])
    if unknown:
        raise ValueError(f"the copy of {table} does not hold: {sorted(unknown)}")
    out = []
    for rid, f in _ROWS[(base, table)]:
        if tree is None or holds(tree, f):
            out.append({"id": rid, "fields": {k: v for k, v in f.items() if (not want or k in want) and v not in (None, "", [], False)}})
    return out


# ---------------------------------------------------------------------------------------------- taking the copy
def fetch_table(token, base, table):
    """A table's records from Airtable with the fields the base has: a field a base lacks (HTTP 422) is left out
    and named. Returns (records, fields left out). A base the token cannot read gives (None, reason)."""
    import requests
    from import_source_library import get_all
    fields, formula = list(TABLES[table][0]), TABLES[table][1]
    dropped = []
    while True:
        try:
            return get_all(token, base, table, fields, formula, copy=False), dropped      # this read is of Airtable itself
        except requests.HTTPError as e:
            code = getattr(e.response, "status_code", None)
            if code in (401, 403, 404):
                return None, f"HTTP {code}"
            if code != 422:
                raise
            body = ""
            try:
                body = e.response.text
            except Exception:
                pass
            bad = next((f for f in fields if f in body), None)
            if bad is None and formula and "RP Metadata Status" in formula and "RP Metadata Status" in fields:
                bad = "RP Metadata Status"
            if bad is None:
                return None, "HTTP 422"
            fields.remove(bad); dropped.append(bad)
            if bad == "RP Metadata Status" and formula:
                formula = "{Data Reviewed}"
            if bad == "Data Reviewed" and formula:
                formula = None


def take(token, commit, out=print, again=False):
    """Copies each table of each base. A table the copy already holds is not read again (a first run of 14 Nov 2026
    was still reading after 80 minutes of its 120: a run that is cut goes on where it stopped) unless again=True."""
    counts = {}
    conn = _connect() if commit else None
    try:
        held = set()
        if commit:
            cur = conn.cursor()
            for stmt in TABLE_SQL.split(";"):
                cur.execute(stmt)
            conn.commit()
            cur.execute("select base, tbl, count(*) from public.gold_records group by 1, 2")
            held = {(b, t): n for b, t, n in cur.fetchall()}
        for name, base in gold_bases().items():
            for table in TABLES:
                if not again and (base, table) in held:
                    counts[(base, table)] = held[(base, table)]
                    out(f"  base {base} {table}: {held[(base, table)]:,} records already in the copy")
                    continue
                recs, note = fetch_table(token, base, table)
                if recs is None:
                    out(f"  base {base} {table}: not read ({note})"); counts[(base, table)] = None
                    continue
                counts[(base, table)] = len(recs)
                out(f"  base {base} {table}: {len(recs):,} records" + (f"; fields the base lacks: {note}" if note else ""))
                if commit and recs:
                    cur = conn.cursor()
                    cur.execute("delete from public.gold_records where base = %s and tbl = %s", (base, table))     # a copy, replaced whole
                    with cur.copy("copy public.gold_records (base, tbl, record_id, fields) from stdin") as cp:
                        for r in recs:
                            cp.write_row((base, table, r["id"], json.dumps(r.get("fields", {}))))
                    conn.commit()
    finally:
        if conn is not None:
            conn.close()
    return counts


VERIFY = [("Metadata", ["Metadata ID", "Selected Value"], "AND({Data Reviewed}, {Selected Value}!='')"),
          ("Metadata", ["Metadata ID", "Source Link"], "AND({Data Reviewed}, {Source Link}&''!='')"),
          ("Metadata", ["Metadata ID", "Evidence", "Page Number", "Source Link", "Selected Value"],
           "AND({Data Reviewed}, {Evidence}!='', {Source Link}!='', OR(FIND('GMC_00048.1', {Metadata ID}),FIND('GMC_00001.1', {Metadata ID})))"),
          ("Metadata", ["Metadata ID", "GMC Type", "Null Value Reasoning"],
           "AND({Data Reviewed}, OR(FIND('Boolean', {GMC Type}&''), FIND('Select', {GMC Type}&'')))"),
          ("Sources", ["Source ID", "Source Link", "Source Year"], None),
          ("Metadata Codes", ["Global Metadata Code (GMC)", "Data Type"], None)]


def verify(token, out=print):
    """The questions the scripts ask, asked of Airtable and of the copy: counts of records in one and not the other."""
    from import_source_library import get_all
    bad = 0
    for name, base in gold_bases().items():
        for table, fields, formula in VERIFY:
            if not serves(base, table, force=True):
                out(f"  base {base} {table}: not in the copy"); continue
            try:
                mine = {r["id"] for r in serve(base, table, fields, formula)}
                theirs = {r["id"] for r in get_all(token, base, table, fields, formula, copy=False)}
            except Exception as e:
                out(f"  base {base} {table}: not compared ({type(e).__name__})"); continue
            only_a, only_c = len(theirs - mine), len(mine - theirs)
            bad += only_a + only_c
            out(f"  base {base} {table} question {VERIFY.index((table, fields, formula)) + 1}: Airtable {len(theirs):,}, the copy {len(mine):,}"
                + (f"; only in Airtable {only_a:,}, only in the copy {only_c:,}" if only_a or only_c else "; the same records"))
    return bad


def main(argv=None):
    ap = argparse.ArgumentParser(description="lanterN's copy of WBA's validated answers")
    ap.add_argument("--take", action="store_true"); ap.add_argument("--verify", action="store_true"); ap.add_argument("--commit", action="store_true")
    ap.add_argument("--again", action="store_true", help="take: read again the tables the copy already holds")
    a = ap.parse_args(argv)
    token = os.environ.get("GOLD_TOKEN", "").strip()
    if not token or not os.environ.get("SUPABASE_DB_URL", "").strip():
        sys.exit("Set GOLD_TOKEN and SUPABASE_DB_URL.")
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    import qa
    run = qa.Run("gold_store.py")
    try:
        counts, bad = {}, 0
        if a.take:
            counts = take(token, a.commit, again=a.again)
            total = sum(v or 0 for v in counts.values())
            print(f">>> {total:,} records in {sum(1 for v in counts.values() if v is not None)} tables" + ("" if a.commit else " (dry run: nothing written)"), flush=True)
        if a.verify:
            bad = verify(token)
            print(f">>> verify: {bad:,} records differ between Airtable and the copy", flush=True)
            if bad:
                run.failure("DATA_CHECK", f"The copy of the validated answers differs from Airtable in {bad} records", key="gold_copy_differs")
    except Exception as e:
        run.failure("RUN_FAILED", f"gold_store.py stopped: {type(e).__name__}", key="gold_store", message=str(e)[:300])
        run.finish(status="failed")
        raise
    run.finish(status="failed" if bad else "ok", rows_in=sum(v or 0 for v in counts.values()), rows_out=sum(v or 0 for v in counts.values()) if a.commit else 0,
               counters={"records": sum(v or 0 for v in counts.values()), "differ": bad})
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
