#!/usr/bin/env python3
"""
load_scores.py: the companies' benchmark results from the WBA Data API, for lanterN's company card.

The owner wants, beside a search for a company, its scores of the latest benchmark round as the Data Explorer
shows them (data.worldbenchmarkingalliance.org): the score, the place within each benchmark and within its
industry. The Data API (https://data.worldbenchmarkingalliance.org/api/data/v1, "Authorization: Bearer <key>",
one request a second) serves the same datasets as the Explorer; its company ids are our wba_id (PT_...).

First step, this file as it stands: LOOK. It lists the datasets (name, rows, columns) and, for the benchmark
results and the measurement areas, the columns with their types and one sample row, so that the table and the
nightly load are written from what the API really returns and not from a guess. Read-only, about ten requests.
The load into the evidence database follows once the columns are known.

    python load_scores.py [--datasets benchmarks,measurementareas]

Env: WBA_DATA_API_KEY (the Data API key: a repository secret, never in code or logs).
"""
import argparse, json, os, sys, time

BASE = "https://data.worldbenchmarkingalliance.org/api/data/v1"
PAUSE = 1.1                                  # the API allows one request a second per key


def get(path, key, params=None, session=None, sleep=time.sleep):
    """The JSON of GET BASE+path; waits PAUSE first (the rate limit); raises with the status and the API's detail."""
    import requests
    sleep(PAUSE)
    r = (session or requests).get(BASE + path, headers={"Authorization": f"Bearer {key}"}, params=params or {}, timeout=60)
    if r.status_code != 200:
        detail = ""
        try:
            detail = str(r.json().get("detail", ""))[:200]
        except Exception:
            pass
        raise RuntimeError(f"GET {path}: HTTP {r.status_code} {detail}")
    return r.json()


def rows_of(payload):
    """The row objects of a dataset response ({'data': [...], 'meta': {...}}), or of a bare list."""
    if isinstance(payload, dict):
        return payload.get("data") or [], payload.get("meta") or {}
    return payload or [], {}


def describe(cols):
    """'name (type)' for each column of a /cols response, whatever its exact shape."""
    out = []
    for c in cols if isinstance(cols, list) else (cols.get("cols") or cols.get("data") or []):
        out.append(f"{c.get('name')} ({c.get('type')})" if isinstance(c, dict) else str(c))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--datasets", default="benchmarks,measurementareas", help="the datasets whose columns and a sample row are printed")
    a = ap.parse_args()
    key = (os.environ.get("WBA_DATA_API_KEY") or "").strip()
    if not key:
        print("WBA_DATA_API_KEY is not set: nothing looked at. Add the Data API key as a repository secret with that name.")
        return 0
    md = ["### WBA Data API: what it serves", ""]
    try:
        listing = get("/datasets", key)
        items = listing if isinstance(listing, list) else (listing.get("data") or listing.get("datasets") or [])
        md += ["| Dataset | Slug | Rows | Columns | Updated |", "| --- | --- | --: | --: | --- |"]
        for d in items:
            md.append(f"| {d.get('name')} | {d.get('slug')} | {d.get('rows')} | {d.get('cols')} | {d.get('updated')} |")
        for slug in [s.strip() for s in a.datasets.split(",") if s.strip()]:
            cols = describe(get(f"/datasets/{slug}/cols", key))
            rows, meta = rows_of(get(f"/datasets/{slug}", key, {"per_page": 2}))
            md += ["", f"**{slug}**: {len(cols)} columns; meta {json.dumps(meta)[:200]}", "", "Columns: " + ", ".join(cols), ""]
            if rows:
                md += ["Sample row:", "", "```", json.dumps(rows[0], ensure_ascii=False, indent=1)[:2500], "```"]
    except Exception as e:
        md.append(f"Stopped: {e}")
        out = "\n".join(md) + "\n"
        print(out, flush=True)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(out)
        return 1
    out = "\n".join(md) + "\n"
    print(out, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
