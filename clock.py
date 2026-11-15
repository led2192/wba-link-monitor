#!/usr/bin/env python3
"""
clock.py: lanterN's own clock. It starts the scheduled jobs at their time.

Why (15 Nov 2026): GitHub starts scheduled workflows late, by hours, or not at all. On the night of 15 Nov
nothing scheduled started between 02:50 and 06:12 UTC: the night's load, the Library mirror and the nightly
reader did not run until a person started them. The server is always on, so the time is kept there.

How: the Clock workflow runs this on the server, one watch after another (like the Scan now watch). Every 30
seconds it looks at the table server/clock.json (workflow file, times of day in UTC, the inputs to start it
with) and starts what has come due since its last look, with `gh workflow run` and the job's own token. The
time of the last look is kept in a file on the server (CLOCK_STATE), so a change of watch, or a stop of up to
six hours, loses nothing: what came due meanwhile is started when the clock is back. After a longer stop it
starts from now, not to replay a day at once.

A workflow in the table has no schedule of its own any more; it takes the input `as_scheduled` and then does
exactly what its scheduled run did.

  python clock.py --minutes 330          one watch
  python clock.py --list                 the table, with the next time of each entry (starts nothing)
Env: GH_TOKEN and GITHUB_REPOSITORY (to start workflows); CLOCK_STATE (default /srv/lantern/clock.last).
"""
import argparse, json, os, subprocess, sys, time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
TABLE = os.path.join(HERE, "server", "clock.json")
STATE = os.environ.get("CLOCK_STATE", "/srv/lantern/clock.last")
CATCH_UP = timedelta(hours=6)
TICK = 30
TRIES = 3


def table(path=TABLE):
    """The entries, checked: [{workflow, name, at: [(h, m)], inputs}]."""
    out = []
    for e in json.load(open(path, encoding="utf-8")):
        wf = e["workflow"]
        if not wf.endswith(".yml") or "/" in wf:
            raise ValueError(f"not a workflow file: {wf}")
        times = []
        for t in e["at"]:
            h, m = (int(x) for x in t.split(":"))
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError(f"not a time of day: {t}")
            times.append((h, m))
        out.append({"workflow": wf, "name": e.get("name") or wf, "at": sorted(set(times)), "inputs": dict(e.get("inputs") or {})})
    return out


def due(entries, since, until):
    """What came due in (since, until]: [(moment, entry)], oldest first. Times are UTC."""
    out = []
    day = since.date()
    while day <= until.date():
        for e in entries:
            for h, m in e["at"]:
                moment = datetime(day.year, day.month, day.day, h, m, tzinfo=timezone.utc)
                if since < moment <= until:
                    out.append((moment, e))
        day += timedelta(days=1)
    return sorted(out, key=lambda x: (x[0], x[1]["workflow"]))


def start_from(saved, now):
    """Where to look from: the last look when it is recent enough, else now."""
    if saved and timedelta(0) <= now - saved <= CATCH_UP:
        return saved
    return now


def read_state(path=STATE):
    try:
        return datetime.fromisoformat(open(path, encoding="utf-8").read().strip())
    except Exception:
        return None


def write_state(moment, path=STATE):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(moment.isoformat())
        os.replace(tmp, path)
    except OSError as e:
        print(f">>> the clock could not keep its time ({e}): after a restart it starts from now", flush=True)


def command(entry, repo):
    cmd = ["gh", "workflow", "run", entry["workflow"], "--repo", repo, "--ref", "main"]
    for k, v in sorted(entry["inputs"].items()):
        cmd += ["-f", f"{k}={v}"]
    return cmd


def start(entry, repo, run=subprocess.run):
    """True when GitHub took the request."""
    try:
        r = run(command(entry, repo), capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return True
        print(f"    gh said: {(r.stderr or r.stdout or '').strip()[:200]}", flush=True)
    except Exception as e:
        print(f"    {type(e).__name__}", flush=True)
    return False


def watch(entries, minutes, repo, now=lambda: datetime.now(timezone.utc), sleep=time.sleep, starter=start,
          load=read_state, save=write_state, failed=lambda entry, moment: None):
    """One watch. Returns (started, not started)."""
    t = now()
    since = start_from(load(), t)
    if since < t - timedelta(minutes=2):
        print(f">>> catching up from {since:%d %b %H:%M} UTC", flush=True)
    end = t + timedelta(minutes=minutes)
    waiting, n_ok, n_bad = [], 0, 0                # waiting: [(moment, entry, tries)]
    while True:
        t = now()
        waiting += [(m, e, 0) for m, e in due(entries, since, t)]
        since = t
        still = []
        for moment, e, tries in waiting:
            if starter(e, repo):
                n_ok += 1
                print(f"{t:%H:%M} UTC  started {e['name']} (due {moment:%H:%M})", flush=True)
            elif tries + 1 >= TRIES:
                n_bad += 1
                print(f">>> {e['name']} (due {moment:%H:%M}) could not be started after {TRIES} tries", flush=True)
                failed(e, moment)
            else:
                still.append((moment, e, tries + 1))
        waiting = still
        save(t)
        if t >= end:
            return n_ok, n_bad
        sleep(TICK)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=330)
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args(argv)
    entries = table()
    now = datetime.now(timezone.utc)
    if a.list:
        for e in entries:
            nxt = due([e], now, now + timedelta(days=1))[0][0]
            print(f"{e['name']}: {', '.join(f'{h:02d}:{m:02d}' for h, m in e['at'])} UTC; next {nxt:%d %b %H:%M}; inputs {e['inputs'] or 'none'}")
        return 0
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo or not os.environ.get("GH_TOKEN"):
        sys.exit("Set GITHUB_REPOSITORY and GH_TOKEN.")
    import qa
    run = qa.Run("clock.py")
    print(f"the clock keeps {sum(len(e['at']) for e in entries)} times a day for {len(entries)} workflows; this watch lasts {a.minutes:.0f} minutes", flush=True)
    ok, bad = watch(entries, a.minutes, repo,
                    failed=lambda e, m: run.failure("RUN_FAILED", f"the clock could not start {e['workflow']} due at {m:%H:%M} UTC", key=f"clock|{e['workflow']}|{m:%Y-%m-%dT%H:%M}"))
    print(f">>> watch over: {ok} started, {bad} not started.", flush=True)
    run.finish("ok" if not bad else "partial", rows_in=ok + bad, rows_out=ok, counters={"started": ok, "not started": bad})
    return 0


if __name__ == "__main__":
    sys.exit(main())
