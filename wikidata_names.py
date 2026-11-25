"""Can a company be found in Wikidata by its name, when its entry carries neither its LEI nor an ISIN?

wikidata_count.py (25 Nov 2026): by LEI or ISIN, Wikidata holds 60% of the companies lantrNa holds and 58% of those
that file in the European format and are not held, with a website for 59% and 44%. The rest are asked for here by
name: the register's legal name without its legal form ("Porsche AG" -> "Porsche"), through Wikidata's own search
(wbsearchentities), keeping the first hit whose country is the company's and which has an official website.

A match by name can be the wrong company, so the first thing measured is how often it is right, on companies whose
entry is known:

  truth   up to 300 companies held that ARE found by LEI: is the entry found by name the same entry?
  held    the companies held that are not found by LEI: how many get an entry with a website, and does the
          website's host agree with the one lantrNa holds for the company?
  esef    up to 600 of the companies that file in the European format, are not held and are not found by LEI:
          how many get an entry with a website

One search a company, one a second, and one query for every 100 entries found, as lanterN-bot; a 429 or 502 to 504
is waited for once and then that set stops. It writes nothing. Prints counts only, never a name or an address.

    python wikidata_names.py [--truth N] [--esef N]

Env: SUPABASE_DB_URL; AIRTABLE_QA_TOKEN for the run record (QA_COMMIT).
"""
import argparse, collections, os, re, sys, time

API = "https://www.wikidata.org/w/api.php"
SPARQL = "https://query.wikidata.org/sparql"
QID = re.compile(r"^Q[1-9][0-9]*$")
SECOND = {"co", "com", "org", "net", "gov", "ac", "edu", "or", "ne"}          # example.co.uk: three labels make the site


def host_key(url):
    """The site of an address, to compare two: 'https://www.x.example.co.uk/a' -> 'example.co.uk'."""
    h = re.sub(r"^[a-z]+://", "", (url or "").strip().lower()).split("/")[0].split("?")[0].split(":")[0]
    parts = [p for p in h.split(".") if p]
    if len(parts) < 2:
        return ""
    return ".".join(parts[-3:] if len(parts) >= 3 and parts[-2] in SECOND and len(parts[-1]) == 2 else parts[-2:])


def search_name(name):
    """What is typed into the search: the legal name without its legal form, or the name as it is."""
    from company_short_names import short_of
    return short_of(name) or " ".join((name or "").split())


def pick(hits, facts, country):
    """The first hit (in the search's order) that is of the company's country and has a website: (QID, website), or
    (None, None). facts: {QID: (set of country codes, website or None)}."""
    for q in hits:
        isos, site = facts.get(q, (set(), None))
        if site and country and country.upper() in isos:
            return q, site
    return None, None


def facts_query(qids):
    return ("SELECT ?c ?iso ?site WHERE { VALUES ?c { %s } OPTIONAL { ?c wdt:P856 ?site } "
            "OPTIONAL { { ?c wdt:P17/wdt:P297 ?iso } UNION { ?c wdt:P159/wdt:P17/wdt:P297 ?iso } } }") % " ".join("wd:" + q for q in qids if QID.match(q))


def facts_of(answer):
    out = {}
    for b in ((answer or {}).get("results") or {}).get("bindings") or []:
        q = ((b.get("c") or {}).get("value") or "").rsplit("/", 1)[-1]
        isos, site = out.get(q, (set(), None))
        if (b.get("iso") or {}).get("value"):
            isos.add(b["iso"]["value"].upper())
        out[q] = (isos, site or (b.get("site") or {}).get("value"))
    return out


class Asker:
    def __init__(self, sess, ua):
        self.sess, self.ua, self.stopped = sess, ua, None

    def _get(self, do):
        for attempt in (1, 2):
            r = do()
            if r.status_code in (429, 502, 503, 504) and attempt == 1:
                wait = r.headers.get("Retry-After", ""); time.sleep(min(max(int(wait) if wait.isdigit() else 60, 30), 300)); continue
            if r.status_code != 200:
                self.stopped = r.status_code; return {}
            return r.json()
        self.stopped = 429; return {}

    def search(self, text):
        j = self._get(lambda: self.sess.get(API, params={"action": "wbsearchentities", "search": text, "language": "en", "uselang": "en", "type": "item", "limit": "5", "format": "json"},
                                            headers={"User-Agent": self.ua}, timeout=40))
        time.sleep(1.0)
        return [h.get("id") for h in j.get("search") or [] if QID.match(h.get("id") or "")]

    def sparql(self, query):
        j = self._get(lambda: self.sess.post(SPARQL, data={"query": query, "format": "json"}, headers={"User-Agent": self.ua, "Accept": "application/sparql-results+json"}, timeout=90))
        time.sleep(3.0)
        return j


def match(asker, companies):
    """{key: (QID, website)} for companies [(key, name, country)]: a search each, then the entries' facts in batches."""
    hits = {}
    for key, name, country in companies:
        if asker.stopped:
            break
        hits[key] = asker.search(search_name(name))
    qids = sorted({q for v in hits.values() for q in v}); facts = {}
    for i in range(0, len(qids), 100):
        if asker.stopped:
            break
        facts.update(facts_of(asker.sparql(facts_query(qids[i:i + 100]))))
    return {key: pick(hits[key], facts, country) for key, name, country in companies if key in hits}, hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--truth", type=int, default=300)
    ap.add_argument("--esef", type=int, default=600)
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import requests, qa, evidence_db, robot_identity as bot
    import wikidata_count as W, esef_count as E
    run = qa.Run("wikidata_names.py")
    with evidence_db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("select upper(r.lei), r.legal_name, upper(r.country), c.website from public.company_register r join public.companies c on c.wba_id = r.wba_id "
                        "where r.found and r.lei is not null and r.legal_name is not null order by r.lei")
            held = [r for r in cur.fetchall() if W.LEI.match(r[0] or "")]
        conn.rollback()
    sess = requests.Session(); ua = bot.BOT_UA; counters = {}
    # which of the held are found by LEI, and under which entry
    known = {}
    for i in range(0, len(held), 150):
        q = "SELECT ?lei ?c WHERE { VALUES ?lei { %s } ?c wdt:P1278 ?lei }" % " ".join('"%s"' % h[0] for h in held[i:i + 150])
        r = sess.post(SPARQL, data={"query": q, "format": "json"}, headers={"User-Agent": ua, "Accept": "application/sparql-results+json"}, timeout=90)
        for b in (r.json().get("results", {}).get("bindings", []) if r.status_code == 200 else []):
            known.setdefault(b["lei"]["value"], b["c"]["value"].rsplit("/", 1)[-1])
        time.sleep(3.0)
    # 1. truth
    sample = [h for h in held if h[0] in known][::max(1, len(known) // max(a.truth, 1))][:a.truth]
    asker = Asker(sess, ua)
    got, hits = match(asker, [(h[0], h[1], h[2]) for h in sample])
    c = collections.Counter()
    for h in sample:
        if h[0] not in got:
            continue
        q, site = got[h[0]]
        c["asked"] += 1; c["an entry kept"] += bool(q); c["the right entry"] += q == known[h[0]]; c["another entry"] += bool(q) and q != known[h[0]]
        c["the right entry is among the search's five"] += known[h[0]] in hits.get(h[0], [])
    print(f"### Truth: companies held whose entry is known by LEI, matched by name ({c['asked']:,} asked" + (f", stopped by {asker.stopped}" if asker.stopped else "") + ")\n")
    for k in ("the right entry is among the search's five", "an entry kept", "the right entry", "another entry"):
        print(f"- {k}: {c[k]:,}" + (f" ({100 * c[k] / c['asked']:.0f}%)" if c["asked"] else ""))
    if c["an entry kept"]:
        print(f"- of the entries kept, right: {100 * c['the right entry'] / c['an entry kept']:.0f}%")
    print("", flush=True); counters.update({"truth_" + k.replace(" ", "_")[:30]: v for k, v in c.items()})
    # 2. held, not found by LEI
    rest = [h for h in held if h[0] not in known]
    asker = Asker(sess, ua)
    got, _ = match(asker, [(h[0], h[1], h[2]) for h in rest])
    c = collections.Counter()
    for h in rest:
        if h[0] not in got:
            continue
        q, site = got[h[0]]
        c["asked"] += 1; c["an entry with a website"] += bool(q)
        if q and h[3]:
            c["a website to compare"] += 1; c["the same site as lantrNa holds"] += host_key(site) == host_key(h[3]) and bool(host_key(site))
    print(f"### Companies held and not found by LEI, matched by name ({c['asked']:,} asked of {len(rest):,}" + (f", stopped by {asker.stopped}" if asker.stopped else "") + ")\n")
    for k in ("an entry with a website", "a website to compare", "the same site as lantrNa holds"):
        print(f"- {k}: {c[k]:,}")
    if c["a website to compare"]:
        print(f"- of those compared, the same site: {100 * c['the same site as lantrNa holds'] / c['a website to compare']:.0f}%")
    print("", flush=True); counters.update({"held_" + k.replace(" ", "_")[:30]: v for k, v in c.items()})
    # 3. the European filers not held and not found by LEI
    held_leis = {h[0] for h in held}; firms, n = {}, 0
    while a.esef:
        n += 1
        r = sess.get(E.API, params={"page[size]": str(E.SIZE), "page[number]": str(n), "include": "entity"}, headers={"User-Agent": ua, "Accept": "application/vnd.api+json"}, timeout=90)
        j = r.json() if r.status_code == 200 else {}
        if not j.get("data"):
            break
        inc = {(x.get("type"), x.get("id")): x for x in j.get("included") or []}
        for it in j["data"]:
            lei = E.lei_of(it, inc); ref = (((it.get("relationships") or {}).get("entity") or {}).get("data") or {})
            name = ((inc.get((ref.get("type"), ref.get("id"))) or {}).get("attributes") or {}).get("name")
            if lei and name and lei not in held_leis and lei not in firms:
                firms[lei] = (name, E.facts(it)[0])
        time.sleep(E.PAUSE)
    leis = sorted(firms); found = set()
    for i in range(0, len(leis), 150):
        q = "SELECT ?lei WHERE { VALUES ?lei { %s } ?c wdt:P1278 ?lei }" % " ".join('"%s"' % l for l in leis[i:i + 150])
        r = sess.post(SPARQL, data={"query": q, "format": "json"}, headers={"User-Agent": ua, "Accept": "application/sparql-results+json"}, timeout=90)
        found |= {b["lei"]["value"] for b in (r.json().get("results", {}).get("bindings", []) if r.status_code == 200 else [])}
        time.sleep(3.0)
    missing = [l for l in leis if l not in found]
    sample = missing[::max(1, len(missing) // max(a.esef, 1))][:a.esef]
    asker = Asker(sess, ua)
    got, hits = match(asker, [(l, firms[l][0], firms[l][1]) for l in sample])
    c = collections.Counter()
    for l in sample:
        if l in got:
            c["asked"] += 1; c["an entry with a website"] += bool(got[l][0]); c["the search found something"] += bool(hits.get(l))
    print(f"### European filers not held: {len(leis):,} with a name, {len(missing):,} not found by LEI; a sample matched by name ({c['asked']:,} asked" + (f", stopped by {asker.stopped}" if asker.stopped else "") + ")\n")
    for k in ("the search found something", "an entry with a website"):
        print(f"- {k}: {c[k]:,}" + (f" ({100 * c[k] / c['asked']:.0f}%)" if c["asked"] else ""))
    print("\nNothing written.", flush=True); counters.update({"esef_" + k.replace(" ", "_")[:30]: v for k, v in c.items()})
    run.finish("ok", rows_in=len(held) + len(leis), rows_out=0, counters=counters, notes="count only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
