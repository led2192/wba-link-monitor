"""What the public register of legal entities (GLEIF) says about the companies lantrNa holds: who they are, and whose they are.

The owner wants a list of companies of lantrNa's own, wider than the one it started from, and to know which
entity a document belongs to (24 Nov 2026: a question about a bank was answered from the annual report of its UK
subsidiary, held under the group). The register of Legal Entity Identifiers is the open source for both: every
entity's legal name, country and status, and, where the entity declared it, its direct and its ultimate parent.
The data are CC0 (GLEIF's terms of use: no claim of rights on them, no suggestion that GLEIF backs the product).

This first step reads the register for the companies that already carry an LEI (public.companies.lei) and keeps
what it says in public.company_register: is the LEI known, is the entity active, is the registration current, and
who its parents are. It shows the owner (Admin, Register) the LEIs that are wrong or lapsed and the companies that
are another's subsidiary. Building the wider list from the register comes after this skeleton is proven.

How it asks: GLEIF's public API (api.gleif.org/api/v1), no key, as lanterN-bot, at most PER_MINUTE requests a
minute (their limit is 60). Records are asked 100 LEIs a request; a parent is one request, asked only when the
record says a parent record exists. Oldest-checked companies first, LIMIT companies a run.

Dry run by default (--commit writes). Prints counts and the names of the JSON keys it met, never an entity.
"""
import argparse, collections, os, sys, time

API = "https://api.gleif.org/api/v1"
PER_MINUTE = 45
BATCH = 100
LIMIT = 2500
TODO_SQL = """
select c.wba_id, upper(btrim(c.lei)) from public.companies c
left join public.company_register r on r.wba_id = c.wba_id
where upper(btrim(coalesce(c.lei, ''))) ~ '^[A-Z0-9]{18}[0-9]{2}$'
order by r.checked_at nulls first, c.wba_id limit %s"""


def at(d, *path):
    """d[path...] or None, whatever is missing on the way."""
    for k in path:
        d = d.get(k) if isinstance(d, dict) else None
    return d


def parent_link(record, which):
    """('record', url) when the register holds a parent record, ('exception', reason-url) when the entity declared
    why it names none, (None, None) otherwise. which: 'direct-parent' or 'ultimate-parent'."""
    links = at(record, "relationships", which, "links") or {}
    if links.get("lei-record"):
        return "record", links["lei-record"]
    if links.get("reporting-exception"):
        return "exception", links["reporting-exception"]
    return None, None


def entity_of(record):
    """(lei, legal name, country, entity status, registration status) of one record of the API."""
    a = record.get("attributes") or {}
    return ((a.get("lei") or record.get("id") or "").upper(), at(a, "entity", "legalName", "name"), at(a, "entity", "legalAddress", "country"),
            at(a, "entity", "status"), at(a, "registration", "status"))


def latin(s):
    """True when the letters of s are mostly Latin ones."""
    letters = [c for c in (s or "") if c.isalpha()]
    return bool(letters) and sum(1 for c in letters if c < "ɐ") >= 0.7 * len(letters)


def names_of(attributes):
    """(legal name, a name in Latin script or None, former names) of an entity's attributes."""
    e = (attributes or {}).get("entity") or {}
    legal = at(e, "legalName", "name")
    others = [(o.get("name"), str(o.get("type") or "")) for o in (e.get("otherNames") or []) if isinstance(o, dict) and o.get("name")]
    trans = [(o.get("name"), str(o.get("type") or "")) for o in (e.get("transliteratedOtherNames") or []) if isinstance(o, dict) and o.get("name")]
    alt = None
    if legal and not latin(legal):
        alt = next((n for n, _ in trans + others if latin(n)), None)
    former = []
    for n, t in others:
        if "PREVIOUS" in t.upper() and n != legal and n not in former:
            former.append(n)
    return legal, alt, former[:8]


class Asker:
    """GETs with the robot's name, never faster than PER_MINUTE a minute; 429 or 5xx waits and tries twice more."""
    def __init__(self, sess, ua, per_minute=PER_MINUTE, sleep=time.sleep, clock=time.time):
        self.sess, self.ua, self.gap, self.sleep, self.clock, self.last, self.calls = sess, ua, 60.0 / per_minute, sleep, clock, 0.0, 0

    def get(self, url, params=None):
        for i in range(3):
            wait = self.gap - (self.clock() - self.last)
            if wait > 0:
                self.sleep(wait)
            self.last = self.clock(); self.calls += 1
            try:
                r = self.sess.get(url, params=params, headers={"User-Agent": self.ua, "Accept": "application/vnd.api+json"}, timeout=40)
            except Exception:
                self.sleep(5 * (i + 1)); continue
            if r.status_code == 404:
                return None
            if r.status_code == 429 or r.status_code >= 500:
                self.sleep(20 * (i + 1)); continue
            if r.status_code != 200:
                return None
            try:
                return r.json()
            except Exception:
                return None
        return None


def read(asker, todo, seen_keys=None):
    """{wba_id: row} for [(wba_id, lei)]: the record, then each parent the record says exists. A company whose LEI the
    register does not know gets found False. seen_keys collects the names of the keys met, to check the API's shape."""
    out, by_lei = {}, collections.defaultdict(list)
    for wba, lei in todo:
        by_lei[lei].append(wba)
    leis, records = sorted(by_lei), {}
    for i in range(0, len(leis), BATCH):
        part = leis[i:i + BATCH]
        got = asker.get(f"{API}/lei-records", {"filter[lei]": ",".join(part), "page[size]": str(BATCH)})
        for rec in (got or {}).get("data") or []:
            lei = entity_of(rec)[0]
            if lei:
                records[lei] = rec
            if seen_keys is not None:
                seen_keys.update("attributes." + k for k in (rec.get("attributes") or {})); seen_keys.update("relationships." + k for k in (rec.get("relationships") or {}))
                for w in ("direct-parent", "ultimate-parent"):
                    seen_keys.update(f"{w}.links." + k for k in (at(rec, "relationships", w, "links") or {}))
    names = {}                                   # parent url -> (lei, name), asked once
    for lei in leis:
        rec = records.get(lei)
        row = {"lei": lei, "found": rec is not None}
        if rec is not None:
            _, row["name"], row["country"], row["entity_status"], row["registration_status"] = entity_of(rec)
            _, row["name_latin"], row["former"] = names_of(rec.get("attributes"))
            for which, col in (("direct-parent", "direct"), ("ultimate-parent", "ultimate")):
                kind, url = parent_link(rec, which)
                row[col + "_kind"] = kind
                if kind == "record":
                    if url not in names:
                        p = asker.get(url)
                        if p and isinstance(p.get("data"), dict):
                            legal, alt, _ = names_of(p["data"].get("attributes"))
                            names[url] = (entity_of(p["data"])[0], alt or legal)          # a parent named in another script shows its Latin name
                        else:
                            names[url] = (None, None)
                    row[col + "_lei"], row[col + "_name"] = names[url]
        for wba in by_lei[lei]:
            out[wba] = row
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=LIMIT, help="companies read in this run")
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import requests, qa, evidence_db, robot_identity as bot
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("gleif_register.py")
    with evidence_db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(TODO_SQL, (a.limit,)); todo = cur.fetchall()
        conn.rollback()
        asker, keys = Asker(requests.Session(), bot.BOT_UA), set()
        rows = read(asker, todo, keys)
        c = collections.Counter()
        for r in rows.values():
            c["found" if r["found"] else "not_in_the_register"] += 1
            if r["found"]:
                c["entity_" + str(r.get("entity_status") or "none").lower()] += 1
                c["registration_" + str(r.get("registration_status") or "none").lower()] += 1
                c["ultimate_parent_" + str(r.get("ultimate_kind") or "none")] += 1
                c["has_another_as_ultimate_parent"] += bool(r.get("ultimate_lei") and r["ultimate_lei"] != r["lei"])
        print(f"{len(todo):,} companies with an LEI asked in {asker.calls:,} requests: " + ", ".join(f"{k} {n}" for k, n in sorted(c.items()))
              + ("" if a.commit else " (dry run: nothing written)"), flush=True)
        print("keys met: " + ", ".join(sorted(keys)), flush=True)
        if a.commit and rows:
            with conn.cursor() as cur:
                cur.executemany(
                    "insert into public.company_register (wba_id, lei, found, legal_name, country, entity_status, registration_status, direct_parent_lei, direct_parent_name, "
                    "ultimate_parent_lei, ultimate_parent_name, parent_note, name_latin, former_names, pending, checked_at) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, false, now()) "
                    "on conflict (wba_id) do update set lei = excluded.lei, found = excluded.found, legal_name = excluded.legal_name, country = excluded.country, "
                    "entity_status = excluded.entity_status, registration_status = excluded.registration_status, direct_parent_lei = excluded.direct_parent_lei, "
                    "direct_parent_name = excluded.direct_parent_name, ultimate_parent_lei = excluded.ultimate_parent_lei, ultimate_parent_name = excluded.ultimate_parent_name, "
                    "parent_note = excluded.parent_note, name_latin = excluded.name_latin, former_names = excluded.former_names, pending = false, checked_at = now()",
                    [(w, r["lei"], r["found"], r.get("name"), r.get("country"), r.get("entity_status"), r.get("registration_status"), r.get("direct_lei"), r.get("direct_name"),
                      r.get("ultimate_lei"), r.get("ultimate_name"),
                      "declared no parent record" if r.get("ultimate_kind") == "exception" else None, r.get("name_latin"), r.get("former") or []) for w, r in rows.items()])
            conn.commit()
    if a.commit:
        with evidence_db.connect() as conn2:            # the names a company is also reached by, from what the register now says
            with conn2.cursor() as cur:
                cur.execute("select public.register_names_refresh()"); print(f"{cur.fetchone()[0]:,} names lead to a company held", flush=True)
            conn2.commit()
    run.finish("ok", rows_in=len(todo), rows_out=len(rows) if a.commit else 0, counters={"requests": asker.calls, **dict(c)}, notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
