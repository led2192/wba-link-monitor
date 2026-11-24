"""Who hangs from whom, for every entity lantrNa knows of, from the register's own file of relationships.

The owner asked to see all the branches of a company's group (24 Nov 2026). gleif_children.py knows which entities
are one level below a parent and which are somewhere in its group, not the levels between. Asking each entity for its
own children is one request an entity and takes many hours (tried the same day, and stopped). GLEIF publishes every
relationship record of the register as one file, the "golden copy" of the relationship records, renewed several times
a day (CC0). This script fetches the latest one, reads it in a stream, and for every entity of public.register_entities
takes its active direct parent (IS_DIRECTLY_CONSOLIDATED_BY) and ultimate parent (IS_ULTIMATELY_CONSOLIDATED_BY).

Since 24 Nov 2026 it also brings every entity of the groups lantrNa holds, not only the 600 a parent that
gleif_children.py keeps: a bank's UK subsidiary was not among its first 600 and so its report could not be told as
the subsidiary's. The relationship file says which entities belong to which group; the register's full file of
entities (several hundred megabytes, read in a stream) gives their names, former names, country and status. And for
a company whose registration is a duplicate or retired, the LEI the register kept in its place (the successor).

Two requests for the indexes and two downloads, as lanterN-bot, once a week.
Dry run by default (--commit writes). Prints counts and the names of keys and columns, never an entity.
"""
import argparse, collections, csv, io, os, re, sys, tempfile, zipfile

INDEX = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/rr/latest"
LEI_INDEX = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/lei2/latest"
OTHER = re.compile(r"^Entity\.OtherEntityNames\.OtherEntityName\.(\d+)$")
TRANS = re.compile(r"^Entity\.TransliteratedOtherEntityNames\.TransliteratedOtherEntityName\.(\d+)$")
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
        if (wanted is None or a in wanted) and b and a != b:
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


def members_of(direct, ultimate, tops):
    """{lei: (direct parent, ultimate parent)} of every entity that belongs to a group topped by one of tops: its
    ultimate or its direct parent is one of them. direct, ultimate: every active relationship of the file."""
    out = {}
    for lei, up in ultimate.items():
        if up in tops:
            out[lei] = (direct.get(lei), up)
    for lei, dp in direct.items():
        if dp in tops and lei not in out:
            out[lei] = (dp, ultimate.get(lei))
    return out


def entity_row(r, cols):
    """(legal name, a name in Latin script or None, former names, country, status, successor LEI or None) of one row
    of the register's file of entities. cols: its column names."""
    from gleif_register import latin
    legal = (r.get("Entity.LegalName") or "").strip() or None
    others = [((r.get(c) or "").strip(), (r.get(c + ".type") or "").upper()) for c in cols if OTHER.match(c) and (r.get(c) or "").strip()]
    trans = [(r.get(c) or "").strip() for c in cols if TRANS.match(c) and (r.get(c) or "").strip()]
    alt = next((n for n in trans + [n for n, _ in others] if latin(n)), None) if legal and not latin(legal) else None
    former = []
    for n, t in others:
        if "PREVIOUS" in t and n != legal and n not in former:
            former.append(n)
    succ = next(((r.get(c) or "").strip().upper() for c in cols if c.endswith("SuccessorLEI") and (r.get(c) or "").strip()), None)
    return legal, alt, former[:8], (r.get("Entity.LegalAddress.Country") or "").strip() or None, (r.get("Entity.EntityStatus") or "").strip() or None, succ


def fetch(requests, url, ua):
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        size = 0
        with requests.get(url, headers={"User-Agent": ua}, stream=True, timeout=900) as r:
            r.raise_for_status()
            for chunk in r.iter_content(1 << 20):
                tmp.write(chunk); size += len(chunk)
    return tmp.name, size


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
    ua, head = bot.BOT_UA, {"User-Agent": bot.BOT_UA, "Accept": "application/json"}
    idx = requests.get(INDEX, headers=head, timeout=60)
    link = csv_link(idx.json() if idx.status_code == 200 else {})
    lidx = requests.get(LEI_INDEX, headers=head, timeout=60)
    lei_link = csv_link(lidx.json() if lidx.status_code == 200 else {})
    print(f"indexes: relationships HTTP {idx.status_code}, entities HTTP {lidx.status_code}", flush=True)
    if not link or not lei_link:
        print("a link to a full file as CSV was not found: nothing read", flush=True)
        run.finish("ok", rows_in=0, rows_out=0, counters={"index_status": idx.status_code}, notes="no link")
        return 0
    path, size = fetch(requests, link, ua)
    rows, cols = open_rows(path)
    direct, ultimate, c = parents(rows, None)
    os.unlink(path)
    with evidence_db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("select lei, direct_parent_lei, ultimate_parent_lei from public.company_register where found"); held = cur.fetchall()
            cur.execute("select count(*) from public.register_entities"); before = cur.fetchone()[0]
        conn.rollback()
        held_leis = {l for l, _, _ in held}
        tops = held_leis | {p for _, d, u in held for p in (d, u) if p}
        members = members_of(direct, ultimate, tops)
        print(f"relationships: {size / 1048576:.0f} MB, {sum(c.values()):,} records; {len(tops):,} companies and parents head groups of {len(members):,} entities in all ({before:,} were known here)", flush=True)
        path, size = fetch(requests, lei_link, ua)
        rows, cols = open_rows(path)
        got, succ, n = {}, {}, 0
        for r in rows:
            n += 1
            lei = (r.get("LEI") or "").strip().upper()
            if lei in members or lei in held_leis:
                e = entity_row(r, cols)
                if lei in members:
                    got[lei] = e
                if lei in held_leis and e[5] and e[5] != lei:
                    succ[lei] = e[5]
        want = set(succ.values()); names = {l: (got[l][1] or got[l][0]) for l in want if l in got}
        if want - set(names):                       # a successor that is in no group held: its name from a second reading
            rows, cols = open_rows(path)
            for r in rows:
                lei = (r.get("LEI") or "").strip().upper()
                if lei in want and lei not in names:
                    e = entity_row(r, cols); names[lei] = e[1] or e[0]
        os.unlink(path)
        print(f"entities: {size / 1048576:.0f} MB, {n:,} records; {len(got):,} of the groups' entities found with their names; "
              f"{sum(1 for e in got.values() if e[1]):,} with a name in Latin script added, {sum(1 for e in got.values() if e[2]):,} with former names; "
              f"{len(succ):,} companies held have an LEI the register replaced by another" + ("" if a.commit else " (dry run: nothing written)"), flush=True)
        if a.commit:
            with conn.cursor() as cur:
                batch = [(l, e[0], e[1], e[2], e[3], e[4], members[l][0], members[l][1]) for l, e in got.items()]
                for i in range(0, len(batch), 2000):
                    cur.executemany("insert into public.register_entities (lei, name, name_latin, former_names, country, entity_status, direct_parent_lei, ultimate_parent_lei, read_at) "
                                    "values (%s, %s, %s, %s, %s, %s, %s, %s, now()) on conflict (lei) do update set name = excluded.name, name_latin = excluded.name_latin, "
                                    "former_names = excluded.former_names, country = excluded.country, entity_status = excluded.entity_status, read_at = now(), "
                                    "direct_parent_lei = coalesce(excluded.direct_parent_lei, public.register_entities.direct_parent_lei), "
                                    "ultimate_parent_lei = coalesce(excluded.ultimate_parent_lei, public.register_entities.ultimate_parent_lei)", batch[i:i + 2000])
                    conn.commit()
                cur.execute("update public.company_register set successor_lei = null, successor_name = null where successor_lei is not null")
                cur.executemany("update public.company_register set successor_lei = %s, successor_name = %s where lei = %s", [(s, names.get(s), l) for l, s in succ.items()])
                cur.execute("insert into public.register_groups (lei, direct_total, ultimate_total, read_at) "
                            "select p.lei, (select count(*) from public.register_entities e where e.direct_parent_lei = p.lei), (select count(*) from public.register_entities e where e.ultimate_parent_lei = p.lei), now() "
                            "from (select unnest(%s::text[]) as lei) p "
                            "on conflict (lei) do update set direct_total = greatest(excluded.direct_total, public.register_groups.direct_total), "
                            "ultimate_total = greatest(excluded.ultimate_total, public.register_groups.ultimate_total, excluded.direct_total), deep_at = now(), "
                            "placed = (select count(*) from public.register_entities e where e.ultimate_parent_lei = excluded.lei and e.direct_parent_lei is not null)", (sorted(tops),))
                cur.execute("select public.register_names_refresh()"); print(f"{cur.fetchone()[0]:,} names lead to a company held", flush=True)
            conn.commit()
    run.finish("ok", rows_in=sum(c.values()), rows_out=len(got) if a.commit else 0, counters={"tops": len(tops), "members": len(members), "named": len(got), "replaced_leis": len(succ)},
               notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
