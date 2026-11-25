"""lantrNa's own list of companies: the first candidates, each checked against the companies already held.

The owner (24 and 25 Nov 2026): lantrNa should build its own list beyond the 2,000 it started with; a register's
list brings legal names, which people do not type; documents will be found as today, by watching a company's own
pages; and "check that we add unique companies and not duplicates of what we have".

So a candidate needs an identity (its LEI), the names people use, and a website. This first step takes the
companies that file an annual report in the European format (filings.xbrl.org, each under its LEI), asks Wikidata
by LEI for the name in English, the other names and the official website, and keeps those that have a website.
Each is then checked, in this order, and the first that holds is written down as the reason:

  1. its LEI is a held company's LEI                               -> "held: the same LEI"
  2. its LEI is an entity of a held company's group in the Register -> "in the group of a held company"
     (a subsidiary, a sister or the parent: kept as a candidate, marked, never added unseen)
  3. its website is a held company's website (the same site)       -> "held: the same website"
  4. its name without the legal form is a held company's name,
     legal name, former name or short name                         -> "held: the same name"
  5. another candidate has the same website or Wikidata entry      -> "another candidate: the same website/entry"

Nothing here watches a page or fetches a document: the candidates are a list to look at (public.catalogue). A
company enters lantrNa only by the owner's choice, later.

Dry run by default: counts. --commit writes public.catalogue (an upsert by LEI; a status the owner set is kept).
Prints counts only, never a company, a name or an address.

    python catalogue_build.py [--commit] [--limit N]

Env: SUPABASE_DB_URL; AIRTABLE_QA_TOKEN for the run record (QA_COMMIT).
"""
import argparse, collections, os, re, sys, time

from company_short_names import key_of, short_of
from wikidata_names import host_key

SPARQL = "https://query.wikidata.org/sparql"
BATCH = 120
QUERY = """SELECT ?lei ?c (SAMPLE(?site) AS ?site_) (SAMPLE(?label) AS ?label_) (GROUP_CONCAT(DISTINCT ?alias; separator="|") AS ?aliases) WHERE {
  VALUES ?lei { %s }
  ?c wdt:P1278 ?lei .
  OPTIONAL { ?c wdt:P856 ?site }
  OPTIONAL { ?c rdfs:label ?label FILTER(LANG(?label) = "en") }
  OPTIONAL { ?c skos:altLabel ?alias FILTER(LANG(?alias) = "en") }
} GROUP BY ?lei ?c"""
LEI = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")


def name_keys(*names):
    """The keys a name is compared by: the name folded, and the name without its legal form."""
    out = set()
    for n in names:
        if n and key_of(n):
            out.add(key_of(n))
            if short_of(n):
                out.add(key_of(short_of(n)))
    return {k for k in out if len(k) >= 4}


def entries(answer):
    """{LEI: (QID, website, label, [aliases])} of one answer of Wikidata; the first entry met for an LEI stands."""
    out = {}
    for b in ((answer or {}).get("results") or {}).get("bindings") or []:
        val = lambda k: (b.get(k) or {}).get("value") or None
        lei = val("lei")
        if lei and lei not in out:
            out[lei] = (val("c").rsplit("/", 1)[-1] if val("c") else None, val("site_"), val("label_"),
                        [a for a in (val("aliases") or "").split("|") if a.strip()][:8])
    return out


def check(cand, held):
    """(reason or None, the held company's id or None) for one candidate against what is held.
    cand: dict lei, website, names (a list). held: dict by_lei {LEI: wba_id}, group {LEI: wba_id}, by_host {host: wba_id}, by_name {key: wba_id}."""
    if cand["lei"] in held["by_lei"]:
        return "held: the same LEI", held["by_lei"][cand["lei"]]
    if cand["lei"] in held["group"]:
        return "in the group of a held company", held["group"][cand["lei"]]
    h = host_key(cand.get("website"))
    if h and h in held["by_host"]:
        return "held: the same website", held["by_host"][h]
    for k in sorted(name_keys(*cand.get("names") or [])):
        if k in held["by_name"]:
            return "held: the same name", held["by_name"][k]
    return None, None


def among(cands):
    """Marks the candidates that repeat another candidate's website or Wikidata entry; the first by LEI stands. cands: dicts, changed in place."""
    seen_host, seen_q = {}, {}
    for c in sorted(cands, key=lambda c: c["lei"]):
        if c.get("reason"):
            continue
        h, q = host_key(c.get("website")), c.get("wikidata")
        if h and h in seen_host:
            c["reason"], c["like"] = "another candidate: the same website", seen_host[h]
        elif q and q in seen_q:
            c["reason"], c["like"] = "another candidate: the same entry", seen_q[q]
        else:
            if h:
                seen_host[h] = c["lei"]
            if q:
                seen_q[q] = c["lei"]
    return cands


def held_index(cur):
    cur.execute("select c.wba_id, c.name, c.website, upper(r.lei), r.legal_name, r.name_latin, r.former_names from public.companies c left join public.company_register r on r.wba_id = c.wba_id")
    by_lei, by_host, by_name = {}, {}, {}
    for wba, name, site, lei, legal, latin, former in cur.fetchall():
        if lei:
            by_lei[lei] = wba
        if host_key(site):
            by_host.setdefault(host_key(site), wba)
        for k in name_keys(name, legal, latin, *(former or [])):
            by_name.setdefault(k, wba)
    cur.execute("select upper(e.lei), r.wba_id from public.register_entities e join public.company_register r on r.lei in (e.ultimate_parent_lei, e.direct_parent_lei) where r.found")
    group = {lei: wba for lei, wba in cur.fetchall()}
    cur.execute("select upper(r.direct_parent_lei), upper(r.ultimate_parent_lei), r.wba_id from public.company_register r where r.found")
    for d, u, wba in cur.fetchall():          # the parents of a held company are of its group too
        for p in (d, u):
            if p and p not in by_lei:
                group.setdefault(p, wba)
    return {"by_lei": by_lei, "group": group, "by_host": by_host, "by_name": by_name}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import requests, qa, evidence_db, robot_identity as bot
    import esef_count as E
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("catalogue_build.py")
    sess, ua = requests.Session(), bot.BOT_UA
    # 1. the companies that file in the European format: LEI, name, country
    firms, n = {}, 0
    while True:
        n += 1
        r = sess.get(E.API, params={"page[size]": str(E.SIZE), "page[number]": str(n), "include": "entity"}, headers={"User-Agent": ua, "Accept": "application/vnd.api+json"}, timeout=90)
        if r.status_code == 429:
            time.sleep(90); r = sess.get(E.API, params={"page[size]": str(E.SIZE), "page[number]": str(n), "include": "entity"}, headers={"User-Agent": ua, "Accept": "application/vnd.api+json"}, timeout=90)
        j = r.json() if r.status_code == 200 else {}
        if not j.get("data"):
            break
        inc = {(x.get("type"), x.get("id")): x for x in j.get("included") or []}
        for it in j["data"]:
            lei = E.lei_of(it, inc); ref = (((it.get("relationships") or {}).get("entity") or {}).get("data") or {})
            name = ((inc.get((ref.get("type"), ref.get("id"))) or {}).get("attributes") or {}).get("name")
            if lei and LEI.match(lei) and lei not in firms:
                firms[lei] = (name, E.facts(it)[0])
        time.sleep(E.PAUSE)
    leis = sorted(firms)[:a.limit] if a.limit else sorted(firms)
    # 2. Wikidata by LEI
    wd, status = {}, 200
    for i in range(0, len(leis), BATCH):
        for attempt in (1, 2):
            r = sess.post(SPARQL, data={"query": QUERY % " ".join('"%s"' % l for l in leis[i:i + BATCH]), "format": "json"}, headers={"User-Agent": ua, "Accept": "application/sparql-results+json"}, timeout=90)
            status = r.status_code
            if status in (429, 502, 503, 504) and attempt == 1:
                time.sleep(60); continue
            break
        if status != 200:
            break
        wd.update(entries(r.json())); time.sleep(5.0)
    # 3. candidates with a website, checked
    with evidence_db.connect() as conn:
        with conn.cursor() as cur:
            held = held_index(cur)
        conn.rollback()
        cands = []
        for lei in leis:
            q, site, label, aliases = wd.get(lei, (None, None, None, []))
            if not site:
                continue
            c = {"lei": lei, "legal_name": firms[lei][0], "country": firms[lei][1], "common_name": label, "aliases": aliases, "website": site, "wikidata": q, "like": None,
                 "names": [firms[lei][0], label] + aliases}
            c["reason"], c["held"] = check(c, held)
            cands.append(c)
        among(cands)
        by = collections.Counter(c["reason"] or "new: no company like it is held" for c in cands)
        print(f"### Candidates for lantrNa's own list\n\n{len(firms):,} companies file in the European format; {len(leis):,} asked of Wikidata (the last answer {status}), {len(wd):,} found by LEI, {len(cands):,} with a website.\n")
        for k, v in by.most_common():
            print(f"- {v:,}: {k}")
        print(f"\nCompanies held, to compare with: {len(held['by_lei']):,} LEIs, {len(held['group']):,} entities of their groups, {len(held['by_host']):,} websites, {len(held['by_name']):,} names.", flush=True)
        written = 0
        if a.commit:
            with conn.cursor() as cur:
                cur.executemany("""insert into public.catalogue (lei, legal_name, common_name, aliases, country, website, wikidata, source, duplicate_why, duplicate_of, like_lei, seen_at)
                                   values (%s, %s, %s, %s, %s, %s, %s, 'esef, wikidata', %s, %s, %s, now())
                                   on conflict (lei) do update set legal_name = excluded.legal_name, common_name = excluded.common_name, aliases = excluded.aliases, country = excluded.country,
                                     website = excluded.website, wikidata = excluded.wikidata, duplicate_why = excluded.duplicate_why, duplicate_of = excluded.duplicate_of, like_lei = excluded.like_lei, seen_at = now()""",
                                [(c["lei"], c["legal_name"], c["common_name"], c["aliases"], c["country"], c["website"], c["wikidata"], c["reason"], c.get("held"), c.get("like")) for c in cands])
                written = len(cands)
            conn.commit()
            print(f"\n{written:,} candidates written to public.catalogue.", flush=True)
        else:
            print("\nDry run: nothing written.", flush=True)
    run.finish("ok", rows_in=len(leis), rows_out=written, counters={re.sub(r"[^a-z0-9]+", "_", k.lower())[:40]: v for k, v in by.items()}, notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
