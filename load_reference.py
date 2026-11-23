#!/usr/bin/env python3
"""
load_reference.py: the reference list (terms, norms, regulations, organisations) into the search database.

lantrNa answers "what is double materiality" or "CSRD" with a card from a reference list, without asking a model
(public.reference_lookup in supabase/search.sql). The list is the owner's own: four tables he made before and
outside this pipeline. It is private and is not in this repository. It lives in the private store as one file,
reference/reference.jsonl.gz, one entry a line:

    {"id": "norm:3f2a…", "kind": "norm", "name": "…", "aliases": ["…"], "category": "…", "subcategory": "…",
     "type": "…", "geography": "…", "year": 2015, "url": "https://…", "text_en": "…", "text_es": "…",
     "issuers": ["…"], "related": ["…"], "ai": true}

This script reads the file and upserts public.reference_entries by id. An entry that is no longer in the file is
never deleted: it is marked gone and no longer shown. A file with fewer than half the entries the database shows
is refused (a cut or wrong file must not empty the list). Since 23 Nov 2026 the database is the master copy: an
entry the owner added or corrected in the admin panel (origin 'added', or edited_at set) is left exactly as it is.

    python load_reference.py [--commit]

Dry run prints the counts by kind and what a commit would add, change and mark gone. Nothing of the entries is
printed, only counts. Env: SUPABASE_DB_URL; REFERENCE_FILE (a local file), or TEXT_STORE_REPO + TEXT_STORE_TOKEN
(the private repository); AIRTABLE_QA_TOKEN for the run record (QA_COMMIT=true writes it).
"""
import argparse, collections, gzip, io, json, os, re, sys

KINDS = ("term", "norm", "regulation", "organisation")
STORE_PATH = "reference/reference.jsonl.gz"
TEXT_MAX = 12000
COLUMNS = ("id", "kind", "name", "aliases", "category", "subcategory", "type", "geography", "year", "url", "text_en", "text_es", "issuers", "related", "ai")


def read_bytes():
    """The file's bytes: REFERENCE_FILE when set, else the private repository's copy."""
    path = (os.environ.get("REFERENCE_FILE") or "").strip()
    if path:
        return open(path, "rb").read()
    repo, tok = (os.environ.get("TEXT_STORE_REPO") or "").strip(), (os.environ.get("TEXT_STORE_TOKEN") or "").strip()
    if not (repo and tok):
        sys.exit("Set REFERENCE_FILE, or TEXT_STORE_REPO and TEXT_STORE_TOKEN.")
    import keepalive
    r = keepalive.session().get(f"https://api.github.com/repos/{repo}/contents/{STORE_PATH}",
                                headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github.raw"}, timeout=120)
    if r.status_code != 200:
        sys.exit(f"The private store answered HTTP {r.status_code} for {STORE_PATH}.")
    return r.content


def parse(data):
    """The lines of the file as dicts. Gzip or plain text."""
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    out = []
    for line in io.StringIO(data.decode("utf-8")):
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _text(v, limit):
    v = re.sub(r"[ \t]+", " ", str(v or "").replace("\x00", "")).strip()
    return v[:limit] or None


def _list(v, limit=12, each=160):
    seen = []
    for x in (v if isinstance(v, list) else []):
        x = _text(x, each)
        if x and x not in seen:
            seen.append(x)
    return seen[:limit]


def with_bracket_aliases(name, aliases):
    """"Clean Development Mechanism (CDM)" is asked for as "CDM" and as "Clean Development Mechanism": both become
    aliases when the name ends in a short bracket that starts with a capital or a digit."""
    m = re.match(r"^(.{3,}?)\s*\(([A-Z0-9][A-Za-z0-9&.+/ -]{1,19})\)$", name)
    if not m:
        return aliases
    return _list(aliases + [m.group(2).strip(), m.group(1).strip()], limit=8, each=120)


def entry_rows(entries):
    """([row], skipped): one tuple a valid entry in COLUMNS' order, the first of each id; entries with no id, an
    unknown kind or no name are counted and left out."""
    rows, seen, skipped = [], set(), 0
    for e in entries:
        eid, kind, name = _text(e.get("id"), 80), _text(e.get("kind"), 20), _text(e.get("name"), 300)
        if not (eid and name and kind in KINDS) or eid in seen:
            skipped += 1
            continue
        seen.add(eid)
        year = e.get("year")
        year = year if isinstance(year, int) and 1400 <= year <= 2100 else None
        url = _text(e.get("url"), 500)
        url = url if url and re.match(r"^https?://", url, re.I) else None
        rows.append((eid, kind, name, with_bracket_aliases(name, _list(e.get("aliases"), limit=8, each=120)), _text(e.get("category"), 120),
                     _text(e.get("subcategory"), 120), _text(e.get("type"), 80), _text(e.get("geography"), 120), year, url,
                     _text(e.get("text_en"), TEXT_MAX), _text(e.get("text_es"), TEXT_MAX), _list(e.get("issuers")), _list(e.get("related")),
                     bool(e.get("ai", True))))
    return rows, skipped


def plan(conn, rows):
    """{"new", "changed", "same", "gone", "held"}: what a commit would do, from the ids and contents the database holds."""
    with conn.cursor() as c:
        c.execute("select id, md5(row(kind, name, aliases, category, subcategory, type, geography, year, url, text_en, text_es, issuers, related, ai)::text), gone "
                  "from public.reference_entries where kind <> 'about' and origin = 'list'")     # not the entries about lantrNa (search.sql), not those added in the admin panel
        held = {r[0]: (r[1], r[2]) for r in c.fetchall()}
        c.execute("select id from public.reference_entries where edited_at is not null"); edited = {r[0] for r in c.fetchall()}
        rows = [r for r in rows if r[0] not in edited]                                # a row a person corrected stays as they left it
        c.execute("create temp table if not exists reference_in (like public.reference_entries including defaults) on commit drop")
        c.execute("truncate reference_in")
        c.executemany("insert into reference_in (id, kind, name, name_key, aliases, category, subcategory, type, geography, year, url, text_en, text_es, issuers, related, ai) "
                      "values (%s, %s, %s, '', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                      [(r[0], r[1], r[2]) + r[3:] for r in rows])
        c.execute("select id, md5(row(kind, name, aliases, category, subcategory, type, geography, year, url, text_en, text_es, issuers, related, ai)::text) from reference_in")
        new = dict(c.fetchall())
    live = {i for i, (_, gone) in held.items() if not gone and i not in edited}
    return {"new": sum(1 for i in new if i not in held), "changed": sum(1 for i, h in new.items() if i in held and (held[i][0] != h or held[i][1])),
            "same": sum(1 for i, h in new.items() if i in held and held[i][0] == h and not held[i][1]), "gone": len(live - set(new)), "held": len(live)}


def write(conn):
    """Upserts reference_in (filled by plan, in this transaction) and marks gone what is no longer in the file."""
    with conn.cursor() as c:
        c.execute("""insert into public.reference_entries (id, kind, name, name_key, aliases, alias_keys, category, subcategory, type, geography, year, url,
                                                          text_en, text_es, issuers, related, ai, gone, loaded_at)
                     select i.id, i.kind, i.name, public.reference_key(i.name), i.aliases,
                            coalesce((select array_agg(distinct public.reference_key(a)) from unnest(i.aliases) a where public.reference_key(a) <> ''), '{}'),
                            i.category, i.subcategory, i.type, i.geography, i.year, i.url, i.text_en, i.text_es, i.issuers, i.related, i.ai, false, now()
                     from reference_in i
                     on conflict (id) do update set kind = excluded.kind, name = excluded.name, name_key = excluded.name_key, aliases = excluded.aliases,
                       alias_keys = excluded.alias_keys, category = excluded.category, subcategory = excluded.subcategory, type = excluded.type,
                       geography = excluded.geography, year = excluded.year, url = excluded.url, text_en = excluded.text_en, text_es = excluded.text_es,
                       issuers = excluded.issuers, related = excluded.related, ai = excluded.ai, gone = false, loaded_at = now()""")
        c.execute("update public.reference_entries e set gone = true where not e.gone and e.kind <> 'about' and e.origin = 'list' and e.edited_at is null "
                  "and not exists (select 1 from reference_in i where i.id = e.id)")
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import qa, evidence_db
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("load_reference.py")
    rows, skipped = entry_rows(parse(read_bytes()))
    kinds = collections.Counter(r[1] for r in rows)
    print(f"{len(rows):,} entries read ({', '.join(f'{k} {kinds[k]:,}' for k in KINDS)}); {skipped:,} left out (no id, no name, unknown kind or a repeated id)", flush=True)
    with evidence_db.connect() as conn:
        p = plan(conn, rows)
        print(f"  the database shows {p['held']:,}; a commit adds {p['new']:,}, changes {p['changed']:,}, leaves {p['same']:,} and marks {p['gone']:,} gone", flush=True)
        if p["held"] and len(rows) < p["held"] / 2:
            run.failure("DATA_CHECK", f"reference file with {len(rows)} entries against {p['held']} shown: refused", key=qa.key("DATA_CHECK", "reference_file_short"))
            run.finish("failed", rows_in=len(rows), rows_out=0, counters=dict(p), notes="file refused: fewer than half the entries shown")
            return 1
        if a.commit:
            write(conn)
            print("  written", flush=True)
        else:
            conn.rollback()
    run.finish("ok", rows_in=len(rows), rows_out=(p["new"] + p["changed"]) if a.commit else 0,
               counters={**{f"entries_{k}": kinds[k] for k in KINDS}, **p, "left_out": skipped}, notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
