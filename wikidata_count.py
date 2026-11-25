"""What does Wikidata know of companies by their LEI: a website, the names people use, a size?

The owner's direction for lantrNa's own list of companies (25 Nov 2026): documents are found as they are today, by
watching each company's own web pages; the regulators' repositories (ESEF, EDGAR) only complete. And a register's
list brings legal names, which is not what a person types. So a new company needs, before anything else, its
website and its common names, and the largest of each country come first, which needs a size.

Wikidata (open, CC0) holds the LEI of a company (property P1278) beside its official website (P856), its labels and
other names, employees (P1128), revenue (P2139) and stock exchange (P414). This script asks its public query service
for two sets of LEIs and counts what comes back:

  held   the companies lantrNa's Register holds (public.company_register): large companies, the favourable case
  esef   the companies that file in the European format and are not held (filings.xbrl.org, read as esef_count.py
         does): mostly smaller, the case that matters for growing the list

The first run (25 Nov 2026) found by LEI only 58% of the companies held, which are large and surely in Wikidata:
many entries lack the LEI. So the companies not found by LEI are asked for again by their ISINs (P946), taken from
the register's own ISIN-to-LEI mapping (the file catalogue_count.py reads), up to three an entity.

One query for every 150 LEIs or ISINs, one every 5 seconds, as lanterN-bot; a 429 or a 502 to 504 is waited for and
asked once more, and then the run stops.
It writes nothing. Prints counts only, never a company, a name or an address.

    python wikidata_count.py [--set held|esef|both] [--limit N]

Env: SUPABASE_DB_URL; AIRTABLE_QA_TOKEN for the run record (QA_COMMIT).
"""
import argparse, collections, os, re, sys, time

SPARQL = "https://query.wikidata.org/sparql"
BATCH, PAUSE = 150, 5.0
LEI = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")
QUERY = """SELECT ?lei (SAMPLE(?site) AS ?site_) (SAMPLE(?label) AS ?label_) (COUNT(DISTINCT ?alias) AS ?aliases) (SAMPLE(?emp) AS ?emp_) (SAMPLE(?rev) AS ?rev_) (SAMPLE(?ex) AS ?ex_) WHERE {
  VALUES ?lei { %s }
  ?c wdt:P1278 ?lei .
  OPTIONAL { ?c wdt:P856 ?site }
  OPTIONAL { ?c rdfs:label ?label FILTER(LANG(?label) = "en") }
  OPTIONAL { ?c skos:altLabel ?alias FILTER(LANG(?alias) = "en") }
  OPTIONAL { ?c wdt:P1128 ?emp }
  OPTIONAL { ?c wdt:P2139 ?rev }
  OPTIONAL { ?c wdt:P414 ?ex }
} GROUP BY ?lei"""


ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def query_for(keys, by="lei"):
    """The query for a batch of LEIs, or of ISINs (by="isin"): the same, with the ISIN's property and its own test."""
    ok = LEI if by == "lei" else ISIN
    q = QUERY % " ".join('"%s"' % k for k in keys if ok.match(k))
    return q if by == "lei" else q.replace("wdt:P1278", "wdt:P946")


def tally(answer, c, owner=None, seen=None):
    """Adds one answer of the query service (its JSON) to the counters. Returns the LEIs found. owner: {ISIN: LEI}
    when the batch was of ISINs, so that an entity found by two of its ISINs counts once (seen: those counted)."""
    found = set() if seen is None else seen
    for b in ((answer or {}).get("results") or {}).get("bindings") or []:
        has = lambda k: bool((b.get(k) or {}).get("value"))
        lei = (b.get("lei") or {}).get("value")
        lei = owner.get(lei) if owner is not None else lei
        if not lei or lei in found:
            continue
        found.add(lei); c["in Wikidata"] += 1
        c["with a website"] += has("site_"); c["with a name in English"] += has("label_")
        c["with other names in English"] += (b.get("aliases") or {}).get("value", "0") not in ("0", "")
        c["with employees"] += has("emp_"); c["with revenue"] += has("rev_"); c["with a stock exchange"] += has("ex_")
        c["with a size (employees or revenue)"] += has("emp_") or has("rev_")
    return found


def ask(sess, keys, ua, by="lei"):
    """(status, answer) for one batch; a 429 or a gateway's 502 to 504 is waited for and asked once more."""
    for attempt in (1, 2):
        r = sess.post(SPARQL, data={"query": query_for(keys, by), "format": "json"}, headers={"User-Agent": ua, "Accept": "application/sparql-results+json"}, timeout=90)
        if r.status_code in (429, 502, 503, 504) and attempt == 1:
            wait = r.headers.get("Retry-After", ""); time.sleep(min(max(int(wait) if wait.isdigit() else 60, 30), 300)); continue
        return r.status_code, (r.json() if r.status_code == 200 else {})
    return 429, {}


def count_set(sess, keys, ua, by="lei", owner=None):
    """(counters, the last status, the LEIs found) over the batches of keys."""
    c, status, found = collections.Counter(), 200, set()
    for i in range(0, len(keys), BATCH):
        try:
            status, answer = ask(sess, keys[i:i + BATCH], ua, by)
        except Exception as e:
            status, answer = type(e).__name__, {}
        if status != 200:
            break
        tally(answer, c, owner, found); c["asked"] += len(keys[i:i + BATCH])
        time.sleep(PAUSE)
    return c, status, found


def isins_of(wanted, requests, ua, most=3):
    """{ISIN: LEI} for the LEIs wanted, up to `most` an entity, from the register's ISIN-to-LEI mapping."""
    import catalogue_count as K
    from gleif_relations import open_rows
    i = None
    for accept in ("application/vnd.api+json", "*/*"):
        i = requests.get(K.ISIN_INDEX, headers={"User-Agent": ua, "Accept": accept}, timeout=60)
        if i.status_code == 200:
            break
    link = K.any_link(i.json()) if i is not None and i.status_code == 200 else None
    if not link:
        return {}
    path, _ = K.fetch(requests, link, ua); rows, cols = open_rows(path)
    lei_c, isin_c = K.column(cols, "LEI"), K.column(cols, "ISIN")
    out, per = {}, collections.Counter()
    for r in rows:
        lei = (r.get(lei_c) or "").strip().upper()
        if lei in wanted and per[lei] < most:
            isin = (r.get(isin_c) or "").strip().upper()
            if ISIN.match(isin):
                out[isin] = lei; per[lei] += 1
    os.unlink(path)
    return out


def esef_leis(sess, ua, held, pages=0):
    """The LEIs of the companies that file in the European format and are not held, read as esef_count.py reads them."""
    import esef_count as E
    out, n = set(), 0
    while True:
        n += 1
        if pages and n > pages:
            break
        r = sess.get(E.API, params={"page[size]": str(E.SIZE), "page[number]": str(n), "include": "entity"}, headers={"User-Agent": ua, "Accept": "application/vnd.api+json"}, timeout=90)
        if r.status_code == 429:
            time.sleep(90); r = sess.get(E.API, params={"page[size]": str(E.SIZE), "page[number]": str(n), "include": "entity"}, headers={"User-Agent": ua, "Accept": "application/vnd.api+json"}, timeout=90)
        j = r.json() if r.status_code == 200 else {}
        if not j.get("data"):
            break
        inc = {(x.get("type"), x.get("id")): x for x in j.get("included") or []}
        out |= {l for l in (E.lei_of(it, inc) for it in j["data"]) if l and l not in held}
        time.sleep(E.PAUSE)
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=("held", "esef", "both"), default="both")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import requests, qa, evidence_db, robot_identity as bot
    run = qa.Run("wikidata_count.py")
    with evidence_db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("select upper(lei) from public.company_register where found and lei is not null"); held = sorted({r[0] for r in cur.fetchall() if LEI.match(r[0] or "")})
        conn.rollback()
    sess, counters = requests.Session(), {}
    sets = []
    if a.set in ("held", "both"):
        sets.append(("held by lantrNa", held))
    if a.set in ("esef", "both"):
        sets.append(("filing in the European format, not held", esef_leis(sess, bot.BOT_UA, set(held))))
    todo = []
    for name, leis in sets:
        if a.limit:
            leis = leis[:a.limit]
        c, status, found = count_set(sess, leis, bot.BOT_UA)
        todo.append((name, leis, found, c))
        print(f"### Companies {name}: {len(leis):,} LEIs, {c['asked']:,} asked of Wikidata, the last answer {status}\n")
        for k in ("in Wikidata", "with a website", "with a name in English", "with other names in English", "with a size (employees or revenue)", "with employees", "with revenue", "with a stock exchange"):
            print(f"- {k}: {c[k]:,}" + (f" ({100 * c[k] / c['asked']:.0f}%)" if c["asked"] else ""))
        print("", flush=True)
        counters.update({re.sub(r"[^a-z0-9]+", "_", f"{name[:8]} {k}".lower())[:40]: v for k, v in c.items()})
    # the second pass: those not found by LEI, by their ISINs
    missing = set().union(*[set(leis) - found for _, leis, found, _ in todo]) if todo else set()
    owner = isins_of(missing, requests, bot.BOT_UA) if missing else {}
    print(f"### Second pass, by ISIN: {len(missing):,} companies not found by LEI; {len(set(owner.values())):,} of them have an ISIN in the register's mapping ({len(owner):,} ISINs asked)\n", flush=True)
    for name, leis, found, c1 in todo:
        mine = {i: l for i, l in owner.items() if l in set(leis) - found}
        c, status, more = count_set(sess, sorted(mine), bot.BOT_UA, by="isin", owner=mine)
        n = len(leis)
        print(f"Companies {name}: {len(more):,} more found by ISIN (the last answer {status}), {c['with a website']:,} of them with a website. "
              f"By LEI or ISIN: {len(found) + len(more):,} of {n:,} ({100 * (len(found) + len(more)) / max(n, 1):.0f}%) in Wikidata, "
              f"{c1['with a website'] + c['with a website']:,} ({100 * (c1['with a website'] + c['with a website']) / max(n, 1):.0f}%) with a website, "
              f"{c1['with a size (employees or revenue)'] + c['with a size (employees or revenue)']:,} with a size.\n", flush=True)
        counters.update({re.sub(r"[^a-z0-9]+", "_", f"{name[:8]} isin {k}".lower())[:40]: v for k, v in c.items()})
    print("Nothing written.", flush=True)
    run.finish("ok", rows_in=sum(len(l) for _, l in sets), rows_out=0, counters=counters, notes="count only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
