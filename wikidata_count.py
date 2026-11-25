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

One query for every 150 LEIs, one every 5 seconds, as lanterN-bot; a 429 is waited for once and then the run stops.
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


def query_for(leis):
    return QUERY % " ".join('"%s"' % l for l in leis if LEI.match(l))


def tally(answer, c):
    """Adds one answer of the query service (its JSON) to the counters. Returns the LEIs found."""
    found = set()
    for b in ((answer or {}).get("results") or {}).get("bindings") or []:
        has = lambda k: bool((b.get(k) or {}).get("value"))
        lei = (b.get("lei") or {}).get("value")
        if not lei or lei in found:
            continue
        found.add(lei); c["in Wikidata"] += 1
        c["with a website"] += has("site_"); c["with a name in English"] += has("label_")
        c["with other names in English"] += (b.get("aliases") or {}).get("value", "0") not in ("0", "")
        c["with employees"] += has("emp_"); c["with revenue"] += has("rev_"); c["with a stock exchange"] += has("ex_")
        c["with a size (employees or revenue)"] += has("emp_") or has("rev_")
    return found


def ask(sess, leis, ua):
    """(status, answer) for one batch; a 429 is waited for once."""
    for attempt in (1, 2):
        r = sess.post(SPARQL, data={"query": query_for(leis), "format": "json"}, headers={"User-Agent": ua, "Accept": "application/sparql-results+json"}, timeout=90)
        if r.status_code == 429 and attempt == 1:
            wait = r.headers.get("Retry-After", ""); time.sleep(min(max(int(wait) if wait.isdigit() else 60, 30), 300)); continue
        return r.status_code, (r.json() if r.status_code == 200 else {})
    return 429, {}


def count_set(sess, leis, ua):
    c, status = collections.Counter(), 200
    for i in range(0, len(leis), BATCH):
        try:
            status, answer = ask(sess, leis[i:i + BATCH], ua)
        except Exception as e:
            status, answer = type(e).__name__, {}
        if status != 200:
            break
        tally(answer, c); c["asked"] += len(leis[i:i + BATCH])
        time.sleep(PAUSE)
    return c, status


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
    for name, leis in sets:
        if a.limit:
            leis = leis[:a.limit]
        c, status = count_set(sess, leis, bot.BOT_UA)
        print(f"### Companies {name}: {len(leis):,} LEIs, {c['asked']:,} asked of Wikidata, the last answer {status}\n")
        for k in ("in Wikidata", "with a website", "with a name in English", "with other names in English", "with a size (employees or revenue)", "with employees", "with revenue", "with a stock exchange"):
            print(f"- {k}: {c[k]:,}" + (f" ({100 * c[k] / c['asked']:.0f}%)" if c["asked"] else ""))
        print("", flush=True)
        counters.update({re.sub(r"[^a-z0-9]+", "_", f"{name[:8]} {k}".lower())[:40]: v for k, v in c.items()})
    print("Nothing written.", flush=True)
    run.finish("ok", rows_in=sum(len(l) for _, l in sets), rows_out=0, counters=counters, notes="count only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
