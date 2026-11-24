"""Who hangs from whom, for every entity lantrNa knows of, from the register's own file of relationships.

The owner asked to see all the branches of a company's group (24 Nov 2026). gleif_children.py knows which entities
are one level below a parent and which are somewhere in its group, not the levels between. Asking each entity for its
own children is one request an entity and takes many hours (tried the same day, and stopped). GLEIF publishes every
relationship record of the register as one file, the "golden copy" of the relationship records, renewed several times
a day (CC0). This script fetches the latest one, reads it in a stream, and for every entity of public.register_entities
takes its active direct parent (IS_DIRECTLY_CONSOLIDATED_BY) and ultimate parent (IS_ULTIMATELY_CONSOLIDATED_BY).

One request for the index of the latest file and one download (tens of megabytes), as lanterN-bot, once a week.
Dry run by default (--commit writes). Prints counts and the names of keys and columns, never an entity.
"""
import argparse, collections, csv, io, os, sys, tempfile, zipfile

INDEX = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/rr/latest"
START, END = "Relationship.StartNode.NodeID", "Relationship.EndNode.NodeID"
TYPE, STATUS = "Relationship.RelationshipType", "Relationship.RelationshipStatus"
DIRECT, ULTIMATE = "IS_DIRECTLY_CONSOLIDATED_BY", "IS_ULTIMATELY_CONSOLIDATED_BY"


def csv_link(index):
    """The address of the full file as CSV in the index of the latest publication, or None. Looked for by shape, not by
    one fixed path: the first 'url' under a key named 'csv' inside something named like the full file."""
    def walk(node, under_full, under_csv):
        if isinstance(node, dict):
            if under_full and under_csv and isinstance(node.get("url"), str):
                return node["url"]
            for k, v in node.items():
                got = walk(v, under_full or "full" in str(k).lower(), under_csv or str(k).lower() == "csv")
                if got:
                    return got
        elif isinstance(node, list):
            for v in node:
                got = walk(v, under_full, under_csv)
                if got:
                    return got
        return None
    return walk(index, False, False)


def parents(rows, wanted):
    """({lei: direct parent}, {lei: ultimate parent}, counts) for the entities in wanted, from the file's rows (dicts).
    Only active relationships; the first one met for an entity and a kind stands."""
    direct, ultimate, c = {}, {}, collections.Counter()
    for r in rows:
        kind, status = (r.get(TYPE) or "").strip(), (r.get(STATUS) or "").strip().upper()
        c[f"{kind or 'no type'} / {status or 'no status'}"] += 1
        if status != "ACTIVE" or kind not in (DIRECT, ULTIMATE):
            continue
        a, b = (r.get(START) or "").strip().upper(), (r.get(END) or "").strip().upper()
        if a in wanted and b and a != b:
            (direct if kind == DIRECT else ultimate).setdefault(a, b)
    return direct, ultimate, c


def open_rows(path):
    """The rows of the CSV inside the downloaded zip (or of a plain CSV), as dicts, in a stream; and the column names."""
    if zipfile.is_zipfile(path):
        z = zipfile.ZipFile(path)
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        f = io.TextIOWrapper(z.open(name), encoding="utf-8-sig", newline="")
    else:
        f = open(path, encoding="utf-8-sig", newline="")
    reader = csv.DictReader(f)
    return reader, list(reader.fieldnames or [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import requests, qa, evidence_db, robot_identity as bot
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("gleif_relations.py")
    head = {"User-Agent": bot.BOT_UA, "Accept": "application/json"}
    idx = requests.get(INDEX, headers=head, timeout=60)
    index = idx.json() if idx.status_code == 200 else {}
    def keys(node, pre="", depth=0):
        return [] if not isinstance(node, dict) or depth > 3 else sum(([pre + k] + keys(v, pre + k + ".", depth + 1) for k, v in node.items()), [])
    print(f"index: HTTP {idx.status_code}; keys: " + ", ".join(sorted(set(keys(index)))[:60]), flush=True)
    link = csv_link(index)
    if not link:
        print("no link to the full file as CSV was found in the index: nothing read", flush=True)
        run.finish("ok", rows_in=0, rows_out=0, counters={"index_status": idx.status_code}, notes="no link")
        return 0
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        size = 0
        with requests.get(link, headers={"User-Agent": bot.BOT_UA}, stream=True, timeout=300) as r:
            r.raise_for_status()
            for chunk in r.iter_content(1 << 20):
                tmp.write(chunk); size += len(chunk)
        path = tmp.name
    print(f"file: {size / 1048576:.1f} MB", flush=True)
    with evidence_db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("select lei, direct_parent_lei, ultimate_parent_lei from public.register_entities"); have = {l: (d, u) for l, d, u in cur.fetchall()}
        conn.rollback()
        rows, cols = open_rows(path)
        print("columns: " + ", ".join(cols[:40]), flush=True)
        direct, ultimate, c = parents(rows, set(have))
        os.unlink(path)
        change = [(l, direct.get(l), ultimate.get(l)) for l in have if (direct.get(l) and direct[l] != have[l][0]) or (ultimate.get(l) and ultimate[l] != have[l][1])]
        print(f"{sum(c.values()):,} relationship records ({', '.join(f'{k} {n:,}' for k, n in c.most_common(8))}); {len(have):,} entities known here: "
              f"{len(direct):,} have an active direct parent in the file, {len(ultimate):,} an ultimate one; {len(change):,} rows to change"
              + ("" if a.commit else " (dry run: nothing written)"), flush=True)
        if a.commit and change:
            with conn.cursor() as cur:
                cur.executemany("update public.register_entities set direct_parent_lei = coalesce(%s, direct_parent_lei), ultimate_parent_lei = coalesce(%s, ultimate_parent_lei) where lei = %s",
                                [(d, u, l) for l, d, u in change])
                cur.execute("update public.register_groups g set deep_at = now(), placed = (select count(*) from public.register_entities e where e.ultimate_parent_lei = g.lei and e.direct_parent_lei is not null)")
                cur.execute("select public.register_names_refresh()")
            conn.commit()
    run.finish("ok", rows_in=sum(c.values()), rows_out=len(change) if a.commit else 0, counters={"entities": len(have), "direct": len(direct), "ultimate": len(ultimate), "changed": len(change), "megabytes": round(size / 1048576)},
               notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
