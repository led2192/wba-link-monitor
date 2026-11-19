#!/usr/bin/env python3
"""
fast_lane.py: a document a person sent with the browser extension is in the search within minutes.

Before 15 Nov 2026 a file sent from a browser reached the library in about 15 minutes (browser_uploads.py) and the
search the next night: its text was read by the nightly Build full text, loaded by the next Evidence database
load and indexed by meaning when the Semantic index run reached it. The owner: "I would like that process to be
fast." This script runs the same three steps, for those documents only, one after the other:

  1. full text    build_fulltext.py --ids ...       the pages' text into the text store, with the documents' metadata
  2. load         load_supabase.py --fast-run RUN   those documents and pages into the evidence database (words: searchable)
  3. meaning      semantic_index.py --ids ...       their passages embedded (main reports from 2024 on, as everywhere)

The documents are found, not passed: the files sent in the last three days that became a library row
(browser_uploads.library_id, status taken) and have no text status in the search yet. A run that fails or is
cut is simply done again by the next one; a document whose file Airtable has not finished storing is left for
the next run; one that cannot be read gets its status from step 1 and is not tried again here.

Since 20 Nov 2026 also the documents the search knew and could not read (no file, a timeout, a failed download)
when a person has sent their file since the search last loaded the row: 61 of the 322 files sent in three days
were of that kind, and each waited for the night. Once the lane's load has written the row again it is not taken
a second time, whatever the reading gave.

    python fast_lane.py --dispatch        the watch's side job: if documents wait, start the Fast lane workflow
    python fast_lane.py [--commit]        the workflow: do the three steps (dry run: say what waits)

Env: SUPABASE_DB_URL; for the steps, what each script needs (AIRTABLE_TOKEN, AIRTABLE_BASE, TEXT_STORE_REPO,
TEXT_STORE_TOKEN); GITHUB_RUN_ID names the run's files in the store. Prints library ids, counts and seconds.
"""
import argparse, os, subprocess, sys, time

MAX_DOCS = 200          # a run; the rest wait for the next one, minutes later
WAITING_SQL = """
select b.library_id, min(b.at) from public.browser_uploads b
where b.library_id is not null and b.status = 'taken' and b.at > now() - interval '3 days'
  and not exists (select 1 from public.documents d where d.library_id = b.library_id and d.text_status is not null
                  and (d.text_status = 'ok' or d.loaded_at >= b.at))
group by b.library_id order by min(b.at) limit %s"""


def waiting(cur, limit=MAX_DOCS):
    """[(library_id, sent at)] of the files sent from a browser that are in the library and not in the search."""
    cur.execute("select to_regclass('public.browser_uploads') is not null and to_regclass('public.documents') is not null")
    if not cur.fetchone()[0]:
        return []
    cur.execute(WAITING_SQL, (limit,))
    return cur.fetchall()


def steps(ids, run_id, py=sys.executable):
    """The three commands, in order: (name, command)."""
    joined = ",".join(ids)
    return [("full text", [py, "build_fulltext.py", "--ids", joined, "--commit", "--budget-min", "40"]),
            ("load", [py, "load_supabase.py", "--fast-run", str(run_id), "--commit"]),
            ("meaning", [py, "semantic_index.py", "--ids", joined, "--commit", "--limit", "0", "--minutes", "40"])]


def run_steps(ids, run_id, run=subprocess.run, say=print):
    """Runs the steps; a step that fails stops the lane (the next run starts again). Returns [(name, seconds, code)]."""
    done = []
    for name, cmd in steps(ids, run_id):
        t0 = time.time()
        code = getattr(run(cmd, timeout=3000), "returncode", 0)
        done.append((name, time.time() - t0, code))
        say(f">>> fast lane, {name}: {time.time() - t0:.0f} s" + ("" if code == 0 else f" (failed, exit {code})"), flush=True)
        if code != 0:
            break
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--dispatch", action="store_true", help="start the Fast lane workflow when documents wait, and stop")
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import evidence_db
    with evidence_db.connect() as conn:
        rows = waiting(conn.cursor())
    ids = [r[0] for r in rows]
    if a.dispatch:
        if not ids:
            print("fast lane: nothing waits.", flush=True)
            return 0
        cmd = ["gh", "workflow", "run", "fast_lane.yml", "--ref", "main"] + (["--repo", os.environ["GITHUB_REPOSITORY"]] if os.environ.get("GITHUB_REPOSITORY") else [])
        env = dict(os.environ); env.setdefault("GH_TOKEN", os.environ.get("GITHUB_TOKEN", ""))
        code = subprocess.run(cmd, env=env, timeout=120).returncode
        print(f"fast lane: {len(ids)} documents wait; workflow {'started' if code == 0 else f'not started (gh exit {code})'}.", flush=True)
        return 0 if code == 0 else 1
    import qa
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("fast_lane.py")
    print(f"fast lane: {len(ids)} documents sent from a browser are in the library and not in the search" + (f": {', '.join(ids[:20])}{' ...' if len(ids) > 20 else ''}" if ids else "."), flush=True)
    if not ids or not a.commit:
        run.finish("ok", rows_in=len(ids), rows_out=0, notes="nothing waits" if not ids else "dry run")
        return 0
    t0 = time.time()
    done = run_steps(ids, os.environ.get("GITHUB_RUN_ID") or f"local{os.getpid()}")
    failed = [n for n, _, code in done if code != 0]
    with evidence_db.connect() as conn:
        cur = conn.cursor()
        cur.execute("select count(*) filter (where d.text_status = 'ok'), count(*) filter (where d.text_status is not null), "
                    "count(*) filter (where exists (select 1 from public.chunk_docs c where c.library_id = d.library_id)), "
                    "round(extract(epoch from now() - min(b.at)) / 60) "
                    "from public.documents d join public.browser_uploads b on b.library_id = d.library_id where d.library_id = any(%s)", (ids,))
        ok, tried, meant, oldest = cur.fetchone()
    line = (f"fast lane: {len(ids)} documents; {tried or 0} have a text status ({ok or 0} searchable by their words), {meant or 0} by meaning; "
            f"{(time.time() - t0) / 60:.1f} min ({', '.join(f'{n} {s:.0f} s' for n, s, _ in done)})"
            + (f"; the oldest was sent {int(oldest)} minutes ago" if oldest is not None else ""))
    print(line, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(line + "\n")
    if failed:
        run.failure("RUN_FAILED", f"fast lane step failed: {failed[0]}", key=f"fast_lane|{failed[0]}")
    run.finish("failed" if failed else "ok", rows_in=len(ids), rows_out=int(ok or 0),
               counters={"documents": len(ids), "searchable": int(ok or 0), "by meaning": int(meant or 0), "seconds": {n: round(s) for n, s, _ in done}})
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
