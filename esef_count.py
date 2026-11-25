"""How many companies beyond the list file an annual report in the European single electronic format (ESEF)?

The owner wants lantrNa's own list of companies to grow, and asked where the addresses of the new companies'
documents would come from (24 Nov 2026). In the European Union a listed company files its annual financial report in
ESEF with its national regulator, and XBRL International gathers those filings in an open repository,
filings.xbrl.org, each filed under the company's LEI: the identifier the register of legal entities (GLEIF) and
lantrNa's Register already use. So a filing there is a report with an address, a year and a company, with no site to
crawl.

A first step, to know what there is: it walks the repository's list of filings through its open API (pages of 500,
one request every 10 seconds, as lanterN-bot; after a 429 it waits what is asked, at least a minute, and asks once more; it stops at the
first answer that is still not 200) and counts filings,
companies, countries and years, and how many of the companies lantrNa holds already. It writes nothing.

The API's shape was not known when this was written (the session could not reach it), so fields are looked for by
name and the names found are printed: the first run says what a second step can rely on.

Prints counts and the names of keys only, never a company or an address.

    python esef_count.py [--pages N]

Env: SUPABASE_DB_URL; AIRTABLE_QA_TOKEN for the run record (QA_COMMIT).
"""
import argparse, collections, os, re, sys, time

API = "https://filings.xbrl.org/api/filings"
SIZE, PAUSE = 500, 10.0          # at one request every 2 seconds the repository answered 429 on the 11th page (24 Nov 2026)
LEI = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")


def lei_of(item, included):
    """The LEI a filing is filed under: in its own attributes, or its entity's (by the relationship), or None."""
    def find(attrs):
        for k, v in (attrs or {}).items():
            if isinstance(v, str) and LEI.match(v.strip().upper()) and ("identifier" in k.lower() or "lei" in k.lower()):
                return v.strip().upper()
        return None
    got = find(item.get("attributes"))
    if got:
        return got
    ref = (((item.get("relationships") or {}).get("entity") or {}).get("data") or {})
    ent = included.get((ref.get("type"), ref.get("id")))
    return find((ent or {}).get("attributes")) if ent else None


def facts(item):
    """(country, year, has an address of a report) of one filing, each looked for by the name of its field."""
    a = item.get("attributes") or {}
    country = next((str(v).strip().upper() for k, v in a.items() if k.lower() == "country" and v), None)
    end = next((str(v) for k, v in a.items() if "period" in k.lower() and "end" in k.lower() and v), "")
    year = int(end[:4]) if re.match(r"^(19|20)\d\d", end) else None
    has_url = any("url" in k.lower() and v for k, v in a.items())
    return country, year, has_url


def count(pages, held):
    """Counters over the pages of the API's answer (each a dict with data and, perhaps, included)."""
    c, by_country, by_year, firms = collections.Counter(), collections.Counter(), collections.Counter(), {}
    for page in pages:
        included = {(x.get("type"), x.get("id")): x for x in page.get("included") or []}
        for item in page.get("data") or []:
            c["filings"] += 1
            lei = lei_of(item, included)
            country, year, has_url = facts(item)
            c["with an address"] += 1 if has_url else 0
            if not lei:
                c["with no LEI found"] += 1; continue
            if year:
                by_year[year] += 1
            if lei not in firms:
                firms[lei] = country
    for lei, country in firms.items():
        by_country[country or "no country"] += 1
        c["companies held by lantrNa"] += 1 if lei in held else 0
    c["companies"] = len(firms); c["companies not held"] = len(firms) - c["companies held by lantrNa"]
    return c, by_country, by_year


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=0, help="pages read at most (0 = all)")
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import requests, qa, evidence_db, robot_identity as bot
    run = qa.Run("esef_count.py")
    with evidence_db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("select upper(lei) from public.company_register where lei is not null"); held = {r[0] for r in cur.fetchall()}
        conn.rollback()
    sess, got, n, status, total = requests.Session(), [], 0, 200, None
    while True:
        n += 1
        if a.pages and n > a.pages:
            break
        try:
            r = sess.get(API, params={"page[size]": str(SIZE), "page[number]": str(n), "include": "entity"}, headers={"User-Agent": bot.BOT_UA, "Accept": "application/vnd.api+json"}, timeout=90)
            if r.status_code == 429:          # slow down: wait what is asked, once
                wait = r.headers.get("Retry-After", ""); time.sleep(min(max(int(wait) if wait.isdigit() else 60, 60), 300))
                r = sess.get(API, params={"page[size]": str(SIZE), "page[number]": str(n), "include": "entity"}, headers={"User-Agent": bot.BOT_UA, "Accept": "application/vnd.api+json"}, timeout=90)
            status = r.status_code
            j = r.json() if status == 200 else {}
        except Exception as e:
            status = type(e).__name__; j = {}
        if status != 200 or not j.get("data"):
            break
        if n == 1:
            one = j["data"][0]
            total = next((v for k, v in (j.get("meta") or {}).items() if isinstance(v, int)), None)
            print(f"keys: top {sorted(j)}; meta {sorted(j.get('meta') or {})}; a filing's attributes {sorted(one.get('attributes') or {})}; its relationships {sorted(one.get('relationships') or {})}; "
                  f"included types {sorted({x.get('type') for x in j.get('included') or []})}; an included item's attributes {sorted(((j.get('included') or [{}])[0]).get('attributes') or {})}", flush=True)
        got.append(j)
        if n % 10 == 0:
            print(f"  {n} pages read", flush=True)
        time.sleep(PAUSE)
    c, by_country, by_year = count(got, held)
    print(f"### Filings in the European format (filings.xbrl.org): {len(got)} pages read, the last answer {status}" + (f"; the repository says {total:,} filings" if total else "") + "\n")
    for k in ("filings", "with an address", "with no LEI found", "companies", "companies held by lantrNa", "companies not held"):
        print(f"- {k}: {c[k]:,}")
    print("\nCompanies by country: " + ", ".join(f"{k} {v:,}" for k, v in by_country.most_common(40)))
    print("Filings by year of the period: " + ", ".join(f"{k} {v:,}" for k, v in sorted(by_year.items())))
    print("\nNothing written.", flush=True)
    run.finish("ok", rows_in=c["filings"], rows_out=0, counters={k.replace(" ", "_"): v for k, v in c.items()}, notes=f"count only; last answer {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
