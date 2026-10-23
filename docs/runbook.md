# Runbook

Day-to-day operation through the Airtable interfaces, and what to do when something looks wrong. Nothing here needs code.

## Every morning (five minutes)
1. Open `base_health`, top row. `status` and `attention` say what needs you. Problem = a job failed or the base is over 90% full; Check = over 80% full or nothing new arrived.
2. Open Pipeline QA, Today's cases, after 13:00 Madrid (Claude pre-reads the cards at 12:46). On each card fill `true_type` and `true_year` (always, even when the shown values are right), tick `title_wrong` only when the title is wrong, untick `sure` when guessing. Ignore `verdict`. Details: [guide.md](guide.md) section 7.4.

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

## Pipeline QA failures: not a to-do list
Pipeline QA is where problems are found and counted; the Disclosure Library is where they are fixed. You do not work through failure rows: there are tens of thousands, and each kind has an owner (`who_fixes`). The weekly review fixes gone and wrongly typed pages itself and fixes the rest in code; the daily research works on company gaps; the robot retries blocked sites.
1. What needs you arrives in the Monday notification, "Para ti esta semana": at most 5 items, each with the company and the link (typically a report only a person can download). Do them with the tools under "Add things" and "Fix things".
2. Analyst flags (DOC_FLAGGED) are yours: Analysts interface, Flagged documents.
3. Never fix anything by editing a failure row. The next sync sees the fix in the library and closes the failure itself (`fixed`, or `resolved_silently` after 30 days without recurring).
4. Only status is yours to set on a failure, if you want: `expected` when the behaviour is correct, `wontfix` when it is not worth fixing.

## When something breaks
- **A GitHub job failed:** `base_health.failed_jobs` links it. Re-run it from the Actions tab; scripts are idempotent, a re-run never duplicates.
- **A page "does not open" but works in your browser:** the site blocks robots. Download the report yourself and use `add_file`.
- **The base is filling up:** run Purge discarded rows (dry run first, then `mode=delete` with `confirm=DELETE`).
- **Numbers jumped overnight:** look at `docs/CHANGELOG.md` for what merged the day before, and at Pipeline QA `runs` for the counters of each job.

## Merging a pull request
Read the PR description (what, why, how it was tested) and, for anything touching the sync, the dry-run comment posted on the PR. The Tests check must be green. Merge; the change runs that night.
