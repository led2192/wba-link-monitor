#!/usr/bin/env python3
"""
robot_identity.py: what would change if lanterN's robot said who it is and obeyed robots.txt? Read-only.

Today the fetchers present themselves as an ordinary browser and do not read robots.txt. The owner wants the robot
to have a name (lanterN-bot), a public page saying what it collects and how to ask it to stop (site/bot.html), and
to respect robots.txt (11 Nov 2026). Both can change what we are able to read: some sites refuse any robot they do
not know, and some forbid the pages we watch. So before any fetcher changes, this script measures, on a sample of
the watched pages (one per site, live ones only):

  - the page fetched as today (a browser's name) and as lanterN-bot: how many answer, how many refuse;
  - robots.txt of the site: is this page allowed for lanterN-bot (or for every robot, "*")?

It prints counts and the hosts where the two differ (public addresses of company sites), nothing else. The rule
of the house applies: the new identity becomes the default only if it loses no more than a small share of pages,
or the owner decides the loss is worth it.

    python robot_identity.py [--sample 300] [--seed 1]

Env: AIRTABLE_TOKEN, AIRTABLE_BASE. Writes robot_identity.md.
"""
import argparse, collections, os, random, sys, time
from urllib import robotparser
from urllib.parse import urlsplit

BOT_NAME = "lanterN-bot"
BOT_PAGE = "https://led2192.github.io/wba-link-monitor/bot.html"
BOT_UA = f"Mozilla/5.0 (compatible; {BOT_NAME}/1.0; +{BOT_PAGE})"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0 Safari/537.36")


def outcome(status):
    """'ok', 'refused' (401, 403, 429), 'gone' (404, 410), 'error' (anything else, or no answer)."""
    if status and 200 <= status < 400:
        return "ok"
    if status in (401, 403, 429):
        return "refused"
    if status in (404, 410):
        return "gone"
    return "error"


def allowed(robots_text, url, agent=BOT_NAME):
    """True when robots.txt lets `agent` read `url`. No robots.txt, or an unreadable one, forbids nothing."""
    if not (robots_text or "").strip():
        return True
    rp = robotparser.RobotFileParser()
    rp.parse(robots_text.splitlines())
    return rp.can_fetch(agent, url)


def one_per_host(rows, n, seed):
    """At most n (url, type) pairs, one per host, chosen the same way for the same seed."""
    by_host = {}
    for url, typ in sorted(rows):
        h = (urlsplit(url).hostname or "").lower()
        if h and h not in by_host:
            by_host[h] = (url, typ)
    hosts = sorted(by_host)
    random.Random(seed).shuffle(hosts)
    return [by_host[h] for h in hosts[:n]]


def get_status(sess, url, ua, timeout=25):
    try:
        r = sess.get(url, headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}, timeout=timeout, allow_redirects=True, stream=True)
        code = r.status_code
        r.close()
        return code
    except Exception:
        return 0


def get_robots(sess, url):
    """(text, status) of the site's robots.txt, asked as the robot."""
    p = urlsplit(url)
    try:
        r = sess.get(f"{p.scheme}://{p.netloc}/robots.txt", headers={"User-Agent": BOT_UA}, timeout=20)
        return (r.text if r.status_code == 200 and "<html" not in r.text[:300].lower() else ""), r.status_code
    except Exception:
        return "", 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=300)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    for v in ("AIRTABLE_TOKEN", "AIRTABLE_BASE"):
        if not os.environ.get(v):
            sys.exit(f"Set {v}.")
    import requests
    from base_health import sweep
    rows = [((r.get("fields", {}).get("url") or "").strip(), r.get("fields", {}).get("type") or "")
            for r in sweep("monitored_links", ["url", "type", "monitor", "status"])
            if r.get("fields", {}).get("monitor") and (r.get("fields", {}).get("status") or "") in ("ok", "live", "")]
    sample = one_per_host([x for x in rows if x[0].startswith("http")], a.sample, a.seed)
    print(f"{len(rows):,} watched live pages; {len(sample)} sampled, one per site", flush=True)
    sess = requests.Session()
    c, differ, forbidden = collections.Counter(), [], []
    for i, (url, typ) in enumerate(sample, 1):
        host = (urlsplit(url).hostname or "").lower()
        as_browser = outcome(get_status(sess, url, BROWSER_UA)); time.sleep(0.5)
        as_bot = outcome(get_status(sess, url, BOT_UA))
        text, rs = get_robots(sess, url)
        ok_bot, ok_any = allowed(text, url, BOT_NAME), allowed(text, url, "*")
        c[f"as a browser: {as_browser}"] += 1; c[f"as {BOT_NAME}: {as_bot}"] += 1
        c["robots.txt: there is one" if text else "robots.txt: none or unreadable"] += 1
        c["robots.txt allows the page" if ok_bot else "robots.txt forbids the page"] += 1
        if as_browser == "ok" and as_bot != "ok":
            c["read as a browser, not as the robot"] += 1; differ.append(f"{host} ({as_bot})")
        if as_browser != "ok" and as_bot == "ok":
            c["read as the robot, not as a browser"] += 1
        if not ok_bot:
            forbidden.append(f"{host} ({typ or 'page'}{'' if ok_any else ', every robot'})")
        if as_browser == "ok" and as_bot == "ok" and ok_bot:
            c["kept: read as the robot and allowed"] += 1
        if as_browser == "ok":
            c["read today"] += 1
        if i % 50 == 0:
            print(f"  {i} of {len(sample)}", flush=True)
    today, kept = c["read today"], c["kept: read as the robot and allowed"]
    md = [f"### Robot identity: {len(sample)} watched pages, one per site", "",
          f"Read today (as a browser): {today}. Still read as {BOT_NAME} and allowed by robots.txt: {kept} "
          f"({kept / max(today, 1):.0%} of those read today).", "",
          "| | Pages |", "| --- | --: |"] + [f"| {k} | {v} |" for k, v in sorted(c.items())]
    if differ:
        md += ["", "Read as a browser but not as the robot: " + ", ".join(sorted(differ)[:60]) + ("." if len(differ) <= 60 else f", and {len(differ) - 60} more.")]
    if forbidden:
        md += ["", "Forbidden by robots.txt: " + ", ".join(sorted(forbidden)[:60]) + ("." if len(forbidden) <= 60 else f", and {len(forbidden) - 60} more.")]
    out = "\n".join(md) + "\n"
    open("robot_identity.md", "w").write(out)
    print(out, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
