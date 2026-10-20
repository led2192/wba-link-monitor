#!/usr/bin/env python3
"""
diagnose_blocked.py — why do ~2,000 monitored pages answer 401/403/429, and what can we do about it?

Read-only. For every monitored page whose last check failed with a "refused" code (or a sample),
it asks a few honest questions and sorts the page into one group:

  fixed_by_headers  the page opens when we send the ordinary headers every browser sends
                    (Accept, Accept-Language). Our monitor only sent a User-Agent. Fixable on our side.
  works_now         opens even with the monitor's current request (temporary block, or already fixed)
  page_gone         now answers 404/410: the address is wrong or the page moved
  bot_wall          still refused; the response names the protection (Cloudflare, Akamai, Imperva,
                    AWS WAF, DataDome, PerimeterX...) or the whole site refuses us. Not something we
                    get around.
  page_refused      the site's home page opens but this page is refused: often a wrong/old address

and for each site it also records whether robots.txt allows the page and whether a sitemap is
readable (the robot-friendly door used by the next step, sitemap monitoring).

One request at a time per site, with a pause, so no site sees more than a trickle.
Output: blocked_diagnosis.csv (one row per page) + a summary in the job log and job summary.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE.  Options: --limit N (0 = all), --codes 401,403,429
"""
import argparse, collections, csv, os, re, sys, time, threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlsplit
from urllib import robotparser

import requests

API = "https://api.airtable.com/v0"
TOKEN = os.environ.get("AIRTABLE_TOKEN"); BASE = os.environ.get("AIRTABLE_BASE")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
MONITOR_HEADERS = {"User-Agent": UA}                       # what airtable_monitor.py sends today
NORMAL_HEADERS = {"User-Agent": UA,                         # what any browser sends on a page load
                  "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                  "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip, deflate",
                  "Upgrade-Insecure-Requests": "1"}
REFUSED = {401, 403, 429}
PAUSE = 2.0   # seconds between two requests to the same site

WALLS = [("Cloudflare", lambda h, b: "cloudflare" in h.get("server", "") or "cf-ray" in h
          or "cf-mitigated" in h or "just a moment" in b),
         ("Akamai", lambda h, b: "akamai" in h.get("server", "") or ("access denied" in b and "reference #" in b)),
         ("Imperva", lambda h, b: "x-iinfo" in h or "incapsula" in b or "imperva" in b),
         ("AWS WAF", lambda h, b: any(k.startswith("x-amzn-waf") for k in h) or "awswaf" in b),
         ("DataDome", lambda h, b: "x-datadome" in h or "datadome" in b),
         ("PerimeterX", lambda h, b: "perimeterx" in b or "px-captcha" in b),
         ("Sucuri", lambda h, b: "sucuri" in h.get("server", "") or "sucuri" in b),
         ("F5", lambda h, b: "the requested url was rejected" in b),
         ("Vercel", lambda h, b: "vercel" in h.get("server", "") and "challenge" in b)]


def sweep(table, fields, formula):
    url = f"{API}/{BASE}/{quote(table)}"
    params = [("pageSize", "100"), ("filterByFormula", formula)] + [("fields[]", f) for f in fields]
    offset = None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        r = requests.get(url, headers={"Authorization": f"Bearer {TOKEN}"}, params=p, timeout=60)
        r.raise_for_status(); j = r.json()
        yield from j.get("records", [])
        offset = j.get("offset"); time.sleep(0.2)
        if not offset:
            break


class Site:
    """Serialises requests to one host, with a pause between them."""
    locks = collections.defaultdict(threading.Lock)
    last = {}

    @classmethod
    def get(cls, url, headers, timeout=20):
        host = urlsplit(url).hostname or ""
        with cls.locks[host]:
            wait = PAUSE - (time.time() - cls.last.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            try:
                return requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            except requests.RequestException:
                return None
            finally:
                cls.last[host] = time.time()


def wall_of(r):
    if r is None:
        return ""
    h = {k.lower(): str(v).lower() for k, v in r.headers.items()}
    b = (r.text or "")[:20000].lower()
    for name, test in WALLS:
        try:
            if test(h, b):
                return name
        except Exception:
            pass
    return ""


_site_cache = {}


def site_info(url):
    """(home_ok, robots_allows, sitemap_ok) for this page's site; cached per host."""
    sp = urlsplit(url); root = f"{sp.scheme}://{sp.netloc}"
    if root not in _site_cache:
        home = Site.get(root + "/", NORMAL_HEADERS)
        rob = Site.get(root + "/robots.txt", NORMAL_HEADERS)
        rp, sitemaps = None, []
        if rob is not None and rob.status_code == 200 and "html" not in rob.headers.get("Content-Type", ""):
            rp = robotparser.RobotFileParser(); rp.parse(rob.text.splitlines())
            sitemaps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", rob.text)
        sm_ok = False
        for sm in (sitemaps[:2] or [root + "/sitemap.xml"]):
            s = Site.get(sm, NORMAL_HEADERS)
            if s is not None and s.status_code == 200 and "<urlset" in s.text[:5000] + s.text[-5000:] \
                    or (s is not None and s.status_code == 200 and "<sitemapindex" in s.text[:5000]):
                sm_ok = True; break
        _site_cache[root] = (home is not None and home.status_code < 400, rp, sm_ok)
    home_ok, rp, sm_ok = _site_cache[root]
    allowed = "unknown" if rp is None else ("yes" if rp.can_fetch(UA, url) else "no")
    return home_ok, allowed, sm_ok


def diagnose(rec):
    f = rec["fields"]; url = f.get("url", "")
    base = Site.get(url, MONITOR_HEADERS)
    code0 = base.status_code if base is not None else None
    if code0 is not None and code0 < 400:
        group, code1, wall = "works_now", code0, ""
    else:
        norm = Site.get(url, NORMAL_HEADERS)
        code1 = norm.status_code if norm is not None else None
        wall = wall_of(norm) or wall_of(base)
        if code1 is not None and code1 < 400:
            group = "fixed_by_headers"
        elif code1 in (404, 410) or code0 in (404, 410):
            group = "page_gone"
        else:
            group = None
    home_ok, allowed, sm_ok = site_info(url)
    if group is None:
        group = "page_refused" if (home_ok and not wall) else "bot_wall"
    return {"link_id": f.get("link_id", ""), "wba_id": f.get("wba_id", ""), "company": f.get("company_name", ""),
            "type": f.get("type", ""), "url": url, "host": urlsplit(url).hostname or "",
            "code_monitor": code0 or "", "code_normal": code1 or "", "group": group, "protection": wall,
            "home_opens": "yes" if home_ok else "no", "robots_allows": allowed,
            "sitemap_readable": "yes" if sm_ok else "no"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--codes", default="401,403,429")
    a = ap.parse_args()
    if not (TOKEN and BASE):
        sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
    codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    formula = "AND({monitor}, OR(%s))" % ", ".join(f"{{http_status}}&''='{c}'" for c in codes)
    recs = list(sweep("monitored_links", ["link_id", "wba_id", "company_name", "type", "url", "http_status"],
                      formula))
    # spread the work across sites so one big site does not dominate a limited run
    by_host = collections.defaultdict(list)
    for r in recs:
        by_host[urlsplit(r["fields"].get("url", "")).hostname].append(r)
    order = []
    while any(by_host.values()):
        for h in list(by_host):
            if by_host[h]:
                order.append(by_host[h].pop(0))
    if a.limit:
        order = order[:a.limit]
    print(f">>> {len(recs):,} monitored pages refused with {codes}; checking {len(order):,} "
          f"on {len({urlsplit(r['fields'].get('url', '')).hostname for r in order}):,} sites", flush=True)

    rows = []
    with ThreadPoolExecutor(max_workers=24) as ex:
        for i, row in enumerate(ex.map(diagnose, order), 1):
            rows.append(row)
            if i % 100 == 0:
                print(f"  ... {i:,}/{len(order):,}", flush=True)
    with open("blocked_diagnosis.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["url"])
        w.writeheader(); w.writerows(rows)

    g = collections.Counter(r["group"] for r in rows)
    comp = {grp: len({r["wba_id"] for r in rows if r["group"] == grp}) for grp in g}
    walls = collections.Counter(r["protection"] or "(unnamed)" for r in rows if r["group"] == "bot_wall")
    sm = sum(1 for r in rows if r["group"] in ("bot_wall", "page_refused") and r["sitemap_readable"] == "yes")
    still = sum(1 for r in rows if r["group"] in ("bot_wall", "page_refused"))
    lines = ["## Refused pages: diagnosis", "",
             f"Checked **{len(rows):,}** monitored pages that answered {', '.join(codes)}.", "",
             "| group | pages | companies |", "|---|---:|---:|"]
    labels = {"fixed_by_headers": "Opens with normal browser headers (fix on our side)",
              "works_now": "Opens now with our current request", "page_gone": "Page gone (404/410)",
              "page_refused": "Site opens, this page refused (check address)",
              "bot_wall": "Bot wall (not bypassed)"}
    for k in ["fixed_by_headers", "works_now", "page_gone", "page_refused", "bot_wall"]:
        lines.append(f"| {labels[k]} | {g.get(k, 0):,} | {comp.get(k, 0):,} |")
    lines += ["", f"Of the {still:,} still refused, **{sm:,}** are on a site whose sitemap we can read.",
              "", "Bot-wall vendors: " + ", ".join(f"{k} {v:,}" for k, v in walls.most_common())]
    out = "\n".join(lines)
    print(out, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(out + "\n")


if __name__ == "__main__":
    main()
