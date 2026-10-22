# wba-link-monitor: instructions for Claude sessions

This repository runs WBA's document pipeline for the SDG2000 universe (about 2,000 companies): it watches company web pages, harvests their reports into the Airtable base "Disclosure Library", reads and classifies them, and measures how complete each company's record is. One person owns it and does not write code; every change is written by Claude and merged by him.

Read `docs/README.md` first. It indexes everything else.

## House rules for code
- Dry run by default. A script writes only with `--commit` (or `QA_COMMIT=true` for the quality log).
- Deterministic ids, idempotent upserts. Re-running a script must never duplicate a row.
- Never delete. Discard with a reason, or tombstone. Deletions happen only through `inbox.py` (`delete_now`) and `purge_discarded.py`, both archived first.
- Orphans are logged, never fabricated. Never send a string into a link field with `typecast`.
- Offline tests with stubbed network and Airtable for every fix (`tests/`, run by the Tests workflow on every pull request). A bug found in a real site becomes a regression case.
- Complete files, one branch per change, one pull request. Never merge: the owner merges.
- Work from the committed code on `main`, never from memory of it.

## Data rules
- The Source Library bases (2024 `appsrc5xpZH12ezzP`, 2025 `appu5pLRjmq7PC4V3`) are read-only, always. They are read with a separate read-only token.
- The repository is public: no names of colleagues, no internal assessments, no credentials in code, docs or commit messages.
- The Disclosure Library holds at most 500,000 records. Check `base_health` before anything that adds rows in bulk.

## Quality log
- Every script that can name a failure should record it with `qa.py` (`run.failure(code, detail, key=...)`) and finish with `run.finish(...)`. Codes are a closed list in `qa.py`; see `docs/quality.md`.
- The Pipeline QA base (`appJQ2mRtjHjzLq9E`) is separate from the Disclosure Library. Labels written by a person are never overwritten.

## Documentation duty
Every pull request updates the documentation it affects, in the same pull request:
- `docs/CHANGELOG.md`: one dated line per change, newest first, with the PR number.
- `docs/scripts.md` when a script or workflow is added, removed, rescheduled or changes what it writes.
- `docs/decisions.md` when a choice was made between options (what, why, what was rejected).
- `docs/pitfalls.md` when a bug taught something that is costly to relearn.
- `docs/quality.md` when the taxonomy, the QA tables or the review loop change.
Scheduled Claude tasks that change the pipeline follow the same duty.

Write docs in plain English, specific (numbers, dates, file and field names), no em-dashes in prose.
