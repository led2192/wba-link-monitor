#!/usr/bin/env python3
"""
network_compare.py: do company sites answer lanterN's own machine as they answer GitHub's?

The pages are read from a network address, and a site may refuse one address and serve another. Before a job
that reads company sites moves to the rented server (docs/server.md), the same sample of monitored pages is read
from both machines with the monitor's own request, and the answers are compared page by page.

  python network_compare.py --read --sample 400      read the sample from this machine; prints the counts and one
                                                     line "OUTCOMES=<letters>", a letter a page, for --compare
  python network_compare.py --compare A=<letters> B=<letters>   the table of both machines' answers

Read-only: GET requests to the pages, GET to Airtable for the list. One request at a time per site. The sample
is the same on every machine on the same day (pages sorted by address, every n-th one). It prints counts and
classes only, never an address. Letters: o answered (200), r refused (401, 403, 429), g gone (404, 410),
e another HTTP code, t timeout, c no connection, x another error.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE (--read).
"""
import argparse, collections, os, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlsplit

LETTERS = {"o": "answered", "r": "refused", "g": "gone", "e": "other code", "t": "timeout", "c": "no connection",
           "x": "other error"}
# The monitor's own request (airtable_monitor.fetch): what a job on either machine sends.
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; WBA-LinkMonitor/1.0)",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.9"}


def letter(status=None, error=None):
    """One letter for one read."""
    if error:
        return {"timeout": "t", "connection": "c"}.get(error, "x")
    if status == 200:
        return "o"
    if status in (401, 403, 429):
        return "r"
    if status in (404, 410):
        return "g"
    return "e"


def sample(urls, n):
    """The same pages on every machine: sorted by address, every k-th one, at most n."""
    urls = sorted({u.strip() for u in urls if u and u.strip().lower().startswith("http")})
    if n <= 0 or len(urls) <= n:
        return urls
    step = len(urls) / n
    return [urls[int(i * step)] for i in range(n)]


def read_one(session, url, timeout=20):
    import requests
    try:
        r = session.get(url, allow_redirects=True, timeout=timeout, headers=HEADERS, stream=True)
        r.close()
        return letter(status=r.status_code)
    except requests.exceptions.SSLError:
        try:
            import urllib3
            urllib3.disable_warnings()
            r = session.get(url, allow_redirects=True, timeout=timeout, headers=HEADERS, verify=False, stream=True)
            r.close()
            return letter(status=r.status_code)
        except Exception:
            return letter(error="ssl")
    except requests.exceptions.Timeout:
        return letter(error="timeout")
    except requests.exceptions.ConnectionError:
        return letter(error="connection")
    except Exception:
        return letter(error="error")


def read_all(urls, workers=12, reader=read_one):
    """Every page once, in the order given; never two requests to one site at the same moment."""
    import requests
    locks = collections.defaultdict(threading.Lock)
    out = [None] * len(urls)

    def work(i):
        host = urlsplit(urls[i]).netloc.lower()
        with locks[host]:
            out[i] = reader(requests.Session(), urls[i])
            time.sleep(0.2)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, range(len(urls))))
    return "".join(out)


def table(a, b):
    """How the same pages answered machine A and machine B: (lines to print, pages A reads and B is refused,
    pages B reads and A is refused)."""
    n = min(len(a), len(b))
    pairs = collections.Counter(zip(a[:n], b[:n]))
    same = sum(v for (x, y), v in pairs.items() if x == y)
    lines = [f"pages compared: {n:,}; the same answer on both machines: {same:,} ({100 * same // max(n, 1)}%)"]
    for name, s in (("A", a[:n]), ("B", b[:n])):
        c = collections.Counter(s)
        lines.append(f"  machine {name}: " + ", ".join(f"{LETTERS[k]} {c[k]:,}" for k in LETTERS if c[k]))
    for (x, y), v in sorted(pairs.items(), key=lambda kv: -kv[1]):
        if x != y:
            lines.append(f"  A {LETTERS[x]}, B {LETTERS[y]}: {v:,}")
    lost = sum(v for (x, y), v in pairs.items() if x == "o" and y != "o")
    won = sum(v for (x, y), v in pairs.items() if y == "o" and x != "o")
    lines.append(f"answered on A and not on B: {lost:,}; answered on B and not on A: {won:,}")
    return lines, lost, won


def monitored_urls():
    from monitor_core import airtable_request
    base, token = os.environ["AIRTABLE_BASE"], os.environ["AIRTABLE_TOKEN"]
    url = f"https://api.airtable.com/v0/{base}/{quote('monitored_links')}"
    params = {"pageSize": 100, "filterByFormula": "{monitor}=TRUE()", "fields[]": ["url"]}
    out, offset = [], None
    while True:
        if offset:
            params["offset"] = offset
        j = airtable_request("GET", url, {"Authorization": f"Bearer {token}"}, params=params).json()
        out += [r.get("fields", {}).get("url") for r in j.get("records", [])]
        offset = j.get("offset")
        if not offset:
            return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--read", action="store_true")
    ap.add_argument("--sample", type=int, default=400)
    ap.add_argument("--compare", nargs=2, metavar=("A=letters", "B=letters"))
    a = ap.parse_args(argv)
    if a.compare:
        lines, lost, won = table(a.compare[0].split("=", 1)[-1], a.compare[1].split("=", 1)[-1])
        print("\n".join(lines))
        return 0
    if not a.read:
        ap.error("--read or --compare")
    urls = sample(monitored_urls(), a.sample)
    t0 = time.time()
    got = read_all(urls)
    c = collections.Counter(got)
    print(f">>> {len(urls):,} monitored pages read in {time.time() - t0:.0f} s from {os.environ.get('RUNNER_NAME', 'this machine')}: "
          + ", ".join(f"{LETTERS[k]} {c[k]:,}" for k in LETTERS if c[k]))
    print("OUTCOMES=" + got)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as fh:
            fh.write(f"outcomes={got}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
