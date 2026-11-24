"""What the standard-setters name on their own pages that the reference list does not hold: candidates for the list.

The owner's wish (23 Nov 2026): besides watching the corpus for what the list is missing, watch the organisations
that set the standards and frameworks. Each entry of the list may carry the address of its own page
(reference_entries.url). This script reads those pages, one at a time and slowly, and takes what reports take from a
report: a full name followed by its acronym in brackets ("International Sustainability Standards Board (ISSB)"). A
pair the list does not hold, under its name or its acronym, is a candidate, shown in Admin beside the candidates
found in the reports, with the site that names it. The owner adds it or sets it aside. No model, no cost.

How it reads:
  - as lanterN-bot, by name, and only where the site's robots.txt allows that page. A page a site forbids or refuses
    (401, 403, 429) is recorded as such and left alone: no other name is tried, no other route.
  - one request for robots.txt and one for the page, PAUSE seconds between two pages of the same site, LIMIT pages a
    run, the pages not read for longest first. HTML only, at most MAX_BYTES of it.
  - a pair is kept when it is new to the list. The pairs of each page are remembered (public.reference_watch), so a
    pair that appears on a page after its first reading is marked new on the site.

Rows the owner set aside or added are never touched, and neither are the candidates found in the reports (a pair
both name keeps its report counts and gains the site). Writes public.reference_watch and public.reference_candidates
only with --commit. Prints counts only: never a name, an address or a host.
"""
import argparse, collections, html as htmllib, os, re, sys, time
from urllib.parse import urlsplit

LIMIT = 300
PAUSE = 3.0
MAX_BYTES = 1_500_000
KINDS = ("organisation", "norm", "regulation")
DROP = re.compile(r"<(script|style|noscript|svg|template)\b.*?</\1\s*>", re.I | re.S)
TAG = re.compile(r"<[^>]+>")
WATCH_SQL = """
select e.url, min(e.id) from public.reference_entries e
left join public.reference_watch w on w.url = e.url
where not e.gone and e.kind = any(%s) and e.url ~* '^https?://[^ ]+$'
group by e.url, w.checked_at order by w.checked_at nulls first, e.url limit %s"""


def text_of(page):
    """The words of an HTML page a person would read: no scripts, styles or tags, entities resolved, one space."""
    return " ".join(htmllib.unescape(TAG.sub(" ", DROP.sub(" ", page or ""))).split())


def host_of(url):
    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def found_on(page, held, pairs, key_of, legal):
    """{key: (name, acronym)} of the page's "full name (ACRONYM)" pairs that the list does not hold."""
    out = {}
    for name, acr in pairs(text_of(page)):
        k = key_of(name)
        if k and k not in held and key_of(acr) not in held and not legal.search(name):
            out.setdefault(k, (name, acr))
    return out


def read(sess, url, robots, bot):
    """(status, html): 'ok', 'forbidden' (robots.txt), 'refused', 'gone', 'error' or 'not html'. robots: {host: text},
    asked once a host. Never tries again under another name."""
    host = host_of(url)
    if host not in robots:
        robots[host] = bot.get_robots(sess, url)[0]
    if not bot.allowed(robots[host], url):
        return "forbidden", ""
    try:
        r = sess.get(url, headers={"User-Agent": bot.BOT_UA, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"}, timeout=25, allow_redirects=True, stream=True)
        what = bot.outcome(r.status_code)
        if what != "ok":
            r.close(); return what, ""
        if "html" not in (r.headers.get("Content-Type") or "").lower():
            r.close(); return "not html", ""
        body = r.raw.read(MAX_BYTES, decode_content=True); r.close()
        return "ok", body.decode(r.encoding or "utf-8", errors="replace")
    except Exception:
        return "error", ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=LIMIT, help="pages read in this run")
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import requests, qa, evidence_db, robot_identity as bot
    from reference_candidates import pairs, key_of, guess, LEGAL
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("standards_watch.py")
    sess, robots, last, seen, results = requests.Session(), {}, {}, collections.Counter(), []
    with evidence_db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("select name_key, alias_keys from public.reference_entries where kind <> 'about'")
            held = set()
            for nk, aks in cur.fetchall():
                held.add(nk); held.update(aks or [])
            cur.execute(WATCH_SQL, (list(KINDS), a.limit)); todo = cur.fetchall()
            cur.execute("select url, names from public.reference_watch"); before = {u: set(n or []) for u, n in cur.fetchall()}
        conn.rollback()
        for url, entry in todo:
            host = host_of(url)
            wait = PAUSE - (time.time() - last.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            status, page = read(sess, url, robots, bot); last[host] = time.time(); seen[status] += 1
            got = found_on(page, held, pairs, key_of, LEGAL) if status == "ok" else {}
            results.append((url, entry, status, host, got))
        cands, fresh = {}, 0
        for url, entry, status, host, got in results:
            first = url not in before
            for k, (name, acr) in got.items():
                new = (not first) and k not in before[url]
                fresh += new
                c = cands.setdefault(k, {"name": name, "acr": acr, "hosts": set(), "new": False})
                c["hosts"].add(host); c["new"] = c["new"] or new
        print(f"{len(todo):,} pages of the list's own addresses read ({', '.join(f'{k} {n}' for k, n in sorted(seen.items())) or 'none'}); "
              f"{len(cands):,} names with their acronym that the list does not hold, {fresh:,} of them new on a page read before"
              + ("" if a.commit else " (dry run: nothing written)"), flush=True)
        if a.commit:
            with conn.cursor() as cur:
                for url, entry, status, host, got in results:
                    # a page that could not be read this time keeps the names of its last reading
                    cur.execute("insert into public.reference_watch (url, entry, status, names, checked_at) values (%s, %s, %s, %s, now()) "
                                "on conflict (url) do update set entry = excluded.entry, status = excluded.status, checked_at = now(), "
                                "names = case when excluded.status = 'ok' then excluded.names else public.reference_watch.names end",
                                (url, entry, status, sorted(got)))
                for k, c in cands.items():
                    cur.execute("insert into public.reference_candidates (key, name, acronym, kind, companies, documents, source, seen_on, new_on_site) values (%s, %s, %s, %s, 0, 0, 'setter', %s, %s) "
                                "on conflict (key) do update set seen_on = excluded.seen_on, new_on_site = public.reference_candidates.new_on_site or excluded.new_on_site, "
                                "found_at = case when public.reference_candidates.source = 'setter' then now() else public.reference_candidates.found_at end",
                                (k, c["name"], c["acr"], guess(c["name"]), sorted(c["hosts"])[:5], c["new"]))
            conn.commit()
    run.finish("ok", rows_in=len(todo), rows_out=len(cands) if a.commit else 0,
               counters={"pages": len(todo), "candidates": len(cands), "new_on_site": fresh, **{f"page_{k.replace(' ', '_')}": n for k, n in seen.items()}},
               notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
