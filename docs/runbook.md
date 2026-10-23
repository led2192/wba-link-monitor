# Runbook

Day-to-day operation through the Airtable interfaces, and what to do when something looks wrong. Nothing here needs code.

## Every morning (five minutes)
1. Open `base_health`, top row. `status` and `attention` say what needs you. Problem = a job failed or the base is over 90% full; Check = over 80% full or nothing new arrived.
2. Open Pipeline QA, Today's cases. Give a verdict on each case (about 15, ten minutes). Untick `sure` when guessing.

## Add things
- **A report or page you found:** paste the link in `companies.add_link` (one per line). Within minutes the robot checks it is new, files it, scans it and answers in `add_result`.
- **A PDF the site will not let the robot download:** drop it in `companies.add_file`.
- **A new company:** fill `website`. The robot proposes report pages in `suggested_links`; delete wrong lines, tick `approve_suggestions`.
- **Scan a page now:** tick `monitored_links.scan_now`. If you suspect it, write what you see in `scan_concern`; the weekly review answers in `audit_reply`.

## Fix things
- **An analyst flagged a document:** open the Analysts interface, Flagged documents (or Pipeline QA, code DOC_FLAGGED). Read `analyst_flag_note`, fix the row with the tools below, then clear `analyst_flag`. If the analyst was wrong, just clear it.
- **Wrong document type:** change `source_type_check`. The change is captured as a correction and becomes a label.
- **Wrong title or year:** type the real title in `curated_doc_title` (it fixes the year too).
- **Row should not exist:** tick `discard` and pick a `discard_reason`. Reasons are counted and read; they are how rules get fixed.
- **Delete for good:** tick `delete_now` (archived and blocked forever; verified rows are never deleted).
- **Company really has not published:** set `queue_snooze_until`.

## Pipeline QA failures: find there, fix in the Disclosure Library
Pipeline QA is where problems are found and counted; the Disclosure Library is where they are fixed. Never fix by editing a failure row.
1. Open Pipeline QA, Open failures (once or twice a week, not daily). Sort by severity. Read `who_fixes`.
2. If it says "Nobody" or "Weekly review", leave it. Those are fixed in code; the weekly review reads them by the hundred, and one code fix closes thousands of rows.
3. If it names you (mainly PAGE_BLOCKED, PAGE_GONE, PAGE_WRONG_TYPE, and a 2023+ report you need now), click `open_in_library`. It opens the page, document or company in the Disclosure Library. Fix it there with the usual tools under "Add things" and "Fix things".
4. Do nothing else. The next sync sees the fix and closes the failure itself (`fixed`, or `resolved_silently` after 30 days without recurring). A type you change in the library becomes a label automatically.
5. Only status is yours to set on a failure: `expected` when the behaviour is correct (a page that legitimately has no report), `wontfix` when it is not worth fixing. Both stop it counting as open.

## When something breaks
- **A GitHub job failed:** `base_health.failed_jobs` links it. Re-run it from the Actions tab; scripts are idempotent, a re-run never duplicates.
- **A page "does not open" but works in your browser:** the site blocks robots. Download the report yourself and use `add_file`.
- **The base is filling up:** run Purge discarded rows (dry run first, then `mode=delete` with `confirm=DELETE`).
- **Numbers jumped overnight:** look at `docs/CHANGELOG.md` for what merged the day before, and at Pipeline QA `runs` for the counters of each job.

## Merging a pull request
Read the PR description (what, why, how it was tested) and, for anything touching the sync, the dry-run comment posted on the PR. The Tests check must be green. Merge; the change runs that night.
