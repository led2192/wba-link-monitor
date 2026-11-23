#!/usr/bin/env python3
"""
job_state.py: copies the runs of the repository's workflows into the evidence database, so the Admin panel's "Jobs"
view (public.admin_jobs in supabase/search.sql) can say what ran, what failed, and which critical job needs a look.

    python job_state.py                    how many runs GitHub has of the last days (writes nothing)
    python job_state.py --commit [--days 3]

Reads: the GitHub API of this repository (the runs of the last --days days), with the workflow's own token.
Writes: public.job_runs, one row a run, by run id (a run seen again is updated, never doubled; no row is removed).
Prints counts only. Env: GH_TOKEN, GITHUB_REPOSITORY, SUPABASE_DB_URL.
"""
import argparse, json, os, subprocess, sys
from datetime import datetime, timedelta, timezone

import qa

PER_PAGE, MAX_PAGES = 100, 15


def gh_runs(repo, since, run=subprocess.run):
    """The runs created since `since` (a date), newest first: [{id, path, name, event, status, conclusion, created_at}]."""
    out = []
    for page in range(1, MAX_PAGES + 1):
        r = run(["gh", "api", f"repos/{repo}/actions/runs?per_page={PER_PAGE}&page={page}&created=>={since:%Y-%m-%d}"],
                capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError("GitHub refused the list of runs")
        rows = (json.loads(r.stdout or "{}").get("workflow_runs")) or []
        out += rows
        if len(rows) < PER_PAGE:
            break
    return out


def as_row(run):
    """(run_id, workflow file, name, event, status, conclusion, started) or None for a run with no file or date."""
    path, started = (run.get("path") or "").split("@")[0], run.get("run_started_at") or run.get("created_at")
    if not path.startswith(".github/workflows/") or not started or not run.get("id"):
        return None
    return (int(run["id"]), path.rsplit("/", 1)[1], (run.get("name") or "")[:120], run.get("event") or "", run.get("status") or "", run.get("conclusion"), started)


UPSERT = """insert into public.job_runs (run_id, workflow, name, event, status, conclusion, started, updated)
            values (%s, %s, %s, %s, %s, %s, %s, now())
            on conflict (run_id) do update set workflow = excluded.workflow, name = excluded.name, event = excluded.event,
              status = excluded.status, conclusion = excluded.conclusion, started = excluded.started, updated = now()"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repo or not os.environ.get("GH_TOKEN"):
        print("Set GITHUB_REPOSITORY and GH_TOKEN; nothing read.")
        return 0
    log = qa.Run("job_state.py")
    since = datetime.now(timezone.utc) - timedelta(days=max(1, min(a.days, 30)))
    try:
        runs = gh_runs(repo, since)
    except Exception as e:
        log.failure("RUN_FAILED", type(e).__name__, key="github")
        log.finish("failed", rows_in=0, rows_out=0)
        print(f"GitHub could not be read ({type(e).__name__}); nothing written.")
        return 1
    rows = [r for r in map(as_row, runs) if r]
    failed = sum(1 for r in rows if r[5] in ("failure", "timed_out", "startup_failure"))
    print(f"{len(rows)} runs of {len({r[1] for r in rows})} workflows since {since:%d %b}: {failed} failed, {sum(1 for r in rows if r[4] != 'completed')} not finished.")
    if not a.commit or not os.environ.get("SUPABASE_DB_URL", "").strip():
        print("dry run: pass --commit (and SUPABASE_DB_URL) to write them.")
        return 0
    import evidence_db
    with evidence_db.connect() as conn:
        cur = conn.cursor()
        cur.executemany(UPSERT, rows)
        cur.execute("select count(*) from public.job_runs")
        print(f"written; public.job_runs holds {cur.fetchone()[0]:,} runs.")
    log.finish("ok", rows_in=len(runs), rows_out=len(rows), counters={"failed": failed})
    return 0


if __name__ == "__main__":
    sys.exit(main())
