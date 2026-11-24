"""The subsidiaries of the companies lantrNa holds, as the public register of legal entities (GLEIF) has them.

Second step on the register (24 Nov 2026). The owner: "it would be good to know whether a company of the list has
subsidiaries or sisters", and to see one company's whole group. gleif_register.py asked who each company is and who
its parents are; this script asks the other way: for each company the register knows, and for each parent of a held
subsidiary (so that its sisters are known), the entities that name it as their parent.

  direct children    the entities whose direct parent it is: one level down
  ultimate children  every entity whose ultimate parent it is: the whole group below it

Each child is kept in public.register_entities with its legal name, a name in Latin script when the legal name is in
another script, its former names, country and status, and under whom it was found. A bank's group holds thousands
of entities: at most CAP children of each kind are kept for one parent, and the true total is recorded beside it
(public.register_groups), so that a drawing can say "and 2,140 more".

GLEIF's public API, no key, as lanterN-bot, at most PER_MINUTE requests a minute; 200 children a request. The
parents not read for longest first, LIMIT parents a run. Dry run by default (--commit writes). Prints counts and the
names of the JSON keys it met, never an entity.
"""
import argparse, collections, os, sys

from gleif_register import API, Asker, at, latin, names_of

PAGE = 200
CAP = 600
LIMIT = 2500
TODO_SQL = """
with parents as (
  select r.lei from public.company_register r where r.found
  union
  select r.ultimate_parent_lei from public.company_register r where r.ultimate_parent_lei is not null
  union
  select r.direct_parent_lei from public.company_register r where r.direct_parent_lei is not null)
select p.lei from parents p left join public.register_groups g on g.lei = p.lei
order by g.read_at nulls first, p.lei limit %s"""


def child_row(record):
    a = record.get("attributes") or {}
    legal, alt, former = names_of(a)
    return {"lei": (a.get("lei") or record.get("id") or "").upper(), "name": legal, "name_latin": alt, "former": former,
            "country": at(a, "entity", "legalAddress", "country"), "status": at(a, "entity", "status")}


def children(asker, lei, kind, cap=CAP, seen_keys=None):
    """(rows, total) of the direct or the ultimate children of lei: at most cap rows, total as the register counts it
    (None when it does not say). kind: 'direct-children' or 'ultimate-children'."""
    rows, total, page = [], None, 1
    while len(rows) < cap:
        got = asker.get(f"{API}/lei-records/{lei}/{kind}", {"page[size]": str(PAGE), "page[number]": str(page)})
        if not got:
            break
        data = got.get("data") or []
        if seen_keys is not None:
            seen_keys.update("meta." + k for k in (got.get("meta") or {})); seen_keys.update("meta.pagination." + k for k in (at(got, "meta", "pagination") or {}))
            for rec in data[:3]:
                seen_keys.update("entity." + k for k in (at(rec, "attributes", "entity") or {}))
        if total is None:
            total = at(got, "meta", "pagination", "total")
        rows += [r for r in (child_row(rec) for rec in data) if r["lei"]]
        last = at(got, "meta", "pagination", "lastPage")
        if not data or len(data) < PAGE or (last is not None and page >= int(last)):
            break
        page += 1
    return rows[:cap], (total if total is not None else len(rows))


DEEP_SQL = """
select g.lei, g.ultimate_total from public.register_groups g
where coalesce(g.ultimate_total, 0) > coalesce(g.direct_total, 0)
order by g.deep_at nulls first, g.lei limit %s"""
DEEP_REQUESTS = 250          # for one group: a bank's thousands of entities are not walked to the end


def walk(asker, first, total, budget=DEEP_REQUESTS):
    """Who hangs from whom below the first level (24 Nov 2026, the owner: "in the detail, see all the branches"). first:
    the LEIs one level below the parent. Each is asked for its own direct children, then those for theirs, level by
    level, until every entity of the group has its place (total), no entity has children left, or the budget of
    requests is spent. [(child row, its direct parent's LEI)] and how many requests it took."""
    placed, out, queue, seen, used = len(first), [], list(first), set(first), 0
    while queue and placed < (total or 0) and used < budget:
        lei = queue.pop(0)
        rows, _ = children(asker, lei, "direct-children", cap=PAGE); used += 1
        for r in rows:
            if r["lei"] in seen:
                continue
            seen.add(r["lei"]); out.append((r, lei)); queue.append(r["lei"]); placed += 1
    return out, used


def walk_groups(a, conn, asker, c):
    """The groups with entities below the first level: place them under their direct parents."""
    with conn.cursor() as cur:
        cur.execute(DEEP_SQL, (a.limit,)); todo = cur.fetchall()
    conn.rollback()
    for n, (lei, total) in enumerate(todo, 1):
        with conn.cursor() as cur:
            cur.execute("select lei from public.register_entities where direct_parent_lei = %s order by lei", (lei,)); first = [r[0] for r in cur.fetchall()]
        conn.rollback()
        found, used = walk(asker, first, total)
        c["groups"] += 1; c["placed_below_the_first_level"] += len(found); c["groups_walked_to_the_end"] += (len(first) + len(found)) >= (total or 0)
        if a.commit:
            with conn.cursor() as cur:
                for r, parent in found:
                    cur.execute("insert into public.register_entities (lei, name, name_latin, former_names, country, entity_status, direct_parent_lei, ultimate_parent_lei, read_at) "
                                "values (%s, %s, %s, %s, %s, %s, %s, %s, now()) on conflict (lei) do update set direct_parent_lei = excluded.direct_parent_lei, "
                                "ultimate_parent_lei = coalesce(public.register_entities.ultimate_parent_lei, excluded.ultimate_parent_lei), read_at = now()",
                                (r["lei"], r["name"], r["name_latin"], r["former"], r["country"], r["status"], parent, lei))
                cur.execute("update public.register_groups set deep_at = now(), placed = %s where lei = %s", (len(first) + len(found), lei))
            conn.commit()
        if n % 50 == 0:
            print(f"  {n:,} of {len(todo):,} groups, {asker.calls:,} requests", flush=True)
    return len(todo)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=LIMIT, help="parents read in this run")
    ap.add_argument("--deep", action="store_true", help="place the entities below the first level under their direct parents, group by group")
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import requests, qa, evidence_db, robot_identity as bot
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("gleif_children.py")
    asker, keys, c = Asker(requests.Session(), bot.BOT_UA), set(), collections.Counter()
    with evidence_db.connect() as conn:
        if a.deep:
            n = walk_groups(a, conn, asker, c)
            print(f"{n:,} groups walked in {asker.calls:,} requests: " + ", ".join(f"{k} {v}" for k, v in sorted(c.items())) + ("" if a.commit else " (dry run: nothing written)"), flush=True)
            if a.commit:
                with conn.cursor() as cur:
                    cur.execute("select public.register_names_refresh()"); print(f"{cur.fetchone()[0]:,} names lead to a company held", flush=True)
                conn.commit()
            run.finish("ok", rows_in=n, rows_out=c["placed_below_the_first_level"] if a.commit else 0, counters={"requests": asker.calls, **dict(c)}, notes="deep, " + ("written" if a.commit else "dry run"))
            return 0
        with conn.cursor() as cur:
            cur.execute(TODO_SQL, (a.limit,)); todo = [r[0] for r in cur.fetchall()]
        conn.rollback()
        for n, lei in enumerate(todo, 1):
            direct, d_total = children(asker, lei, "direct-children", seen_keys=keys)
            deep, u_total = children(asker, lei, "ultimate-children", seen_keys=keys) if d_total else ([], 0)      # no child one level down: none further down either
            c["parents"] += 1; c["with_children"] += bool(d_total or u_total); c["direct_children"] += len(direct); c["ultimate_children"] += len(deep)
            c["capped"] += (d_total or 0) > len(direct) or (u_total or 0) > len(deep)
            c["names_in_latin_added"] += sum(1 for r in direct + deep if r["name_latin"]); c["with_former_names"] += sum(1 for r in direct + deep if r["former"])
            if a.commit:
                with conn.cursor() as cur:
                    d_ids = {r["lei"] for r in direct}
                    for r in {x["lei"]: x for x in deep + direct}.values():
                        cur.execute("insert into public.register_entities (lei, name, name_latin, former_names, country, entity_status, direct_parent_lei, ultimate_parent_lei, read_at) "
                                    "values (%s, %s, %s, %s, %s, %s, %s, %s, now()) on conflict (lei) do update set name = excluded.name, name_latin = excluded.name_latin, "
                                    "former_names = excluded.former_names, country = excluded.country, entity_status = excluded.entity_status, read_at = now(), "
                                    "direct_parent_lei = coalesce(excluded.direct_parent_lei, public.register_entities.direct_parent_lei), "
                                    "ultimate_parent_lei = coalesce(excluded.ultimate_parent_lei, public.register_entities.ultimate_parent_lei)",
                                    (r["lei"], r["name"], r["name_latin"], r["former"], r["country"], r["status"], lei if r["lei"] in d_ids else None,
                                     lei if any(x["lei"] == r["lei"] for x in deep) else None))
                    cur.execute("insert into public.register_groups (lei, direct_total, ultimate_total, direct_kept, ultimate_kept, read_at) values (%s, %s, %s, %s, %s, now()) "
                                "on conflict (lei) do update set direct_total = excluded.direct_total, ultimate_total = excluded.ultimate_total, direct_kept = excluded.direct_kept, "
                                "ultimate_kept = excluded.ultimate_kept, read_at = now()", (lei, d_total, u_total, len(direct), len(deep)))
                conn.commit()
            if n % 200 == 0:
                print(f"  {n:,} of {len(todo):,} parents, {asker.calls:,} requests", flush=True)
    print(f"{len(todo):,} parents asked in {asker.calls:,} requests: " + ", ".join(f"{k} {n}" for k, n in sorted(c.items())) + ("" if a.commit else " (dry run: nothing written)"), flush=True)
    print("keys met: " + ", ".join(sorted(keys)), flush=True)
    if a.commit:
        with evidence_db.connect() as conn2:            # the names a company is also reached by, from what the register now says
            with conn2.cursor() as cur:
                cur.execute("select public.register_names_refresh()"); print(f"{cur.fetchone()[0]:,} names lead to a company held", flush=True)
            conn2.commit()
    run.finish("ok", rows_in=len(todo), rows_out=(c["direct_children"] + c["ultimate_children"]) if a.commit else 0, counters={"requests": asker.calls, **dict(c)}, notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
