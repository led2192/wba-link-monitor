# Pipeline QA

The pipeline runs unattended and nobody reviews the 275,000 documents by hand, so its accuracy has to be measured, not assumed. Pipeline QA does three things: it records every failure the pipeline can name (one row per distinct failure, not per occurrence), it builds a labelled truth set a little every day, and it scores every change to a rule or a prompt against that truth set before it is merged.

Design document (21 Oct 2026, inventory of all 34 scripts at the time, the reasoning behind each choice): https://claude.ai/artifact/Qkz8e2Sn8PWtooz1YStPXf

## The base: Pipeline QA (`appJQ2mRtjHjzLq9E`)

| Table | One row per | Written by |
| --- | --- | --- |
| `runs` | script execution (`run_key` = workflow + GitHub run id) | `qa.Run.finish()` |
| `failures` | distinct failure (`failure_key` = sha1 of code + key); `occurrences`, `last_seen`, `last_commit` move on each recurrence | `qa.Run.failure()`, `qa_sync.py` |
| `labels` | one judged fact about one document, page or company (the golden set) | the daily sampler and review, corrections, Source Library, audits; the owner's verdicts |
| `evals` | one scoring of one rule or prompt against the labels | `eval.py` |

Interface "Pipeline QA": **Today's cases** (the daily batch, one card per case), **Open failures** (by code, severity, stage), **Runs**.

Every field carries a description in Airtable. Rules: a failure key that does not recur for 30 days is closed as `resolved_silently` with the last commit seen; a failure marked `fixed` that recurs reopens itself with a note; a label a person wrote is never overwritten.

## Failure codes (closed list, v1)

| Code | Means | Main source |
| --- | --- | --- |
| PAGE_UNREACHABLE | page could not be fetched (timeout, ssl, connection, no response) | monitor status error / dead with other codes |
| PAGE_GONE | 404 or 410 | monitor status dead |
| PAGE_BLOCKED | the site refuses robots (one row per domain) | monitor status blocked |
| PAGE_EMPTY | a report page with no document links | possible_issue |
| PAGE_UNHARVESTED | 3+ document links on the page not held | harvest page health |
| PAGE_WRONG_TYPE | a report page typed so it is not harvested | audits, add_link retype |
| DOC_FETCH_FAIL | file not attached (blocked, gone, too_big, not_pdf, download_fail) | file_status, cover_status |
| DOC_UNREADABLE | file held, no text (no_text, parse_fail, truncated) | cover_status |
| DOC_DROPPED | a link seen but not turned into a row | harvest, doc_links (to instrument) |
| DOC_DUPLICATE | two rows, one document | discard_reason |
| DOC_WRONG_ROW | the row should not exist (misattributed, not a disclosure, out of scope) | discard_reason |
| TYPE_DISAGREE | URL type and AI type differ and the choice moves a completeness check | type_mismatch + stakes rule |
| TYPE_MISSING | no effective type 7+ days after arrival (non_english, no_cover, pending) | qa_sync |
| LABEL_CORRECTION | a person changed what the robot wrote | robot_* snapshots |
| CO_GAP | a completeness check fails or a report is overdue | gap_pages, overdue_reports |
| CO_MISSED_KNOWN | a document known to exist is not in the library | Source Library matching, research, audits (to build) |
| RUN_FAILED | a job or a write did not complete | base_health, `qa.record_exception()` |

Document rows are written only for report-shaped documents (a report-family type, or a report word plus a 2023+ year); the rest is counted in the run's counters. Severity is High when the failure hides or costs a 2023+ sustainability or annual report, or blocks a whole domain.

## Using it from a script

```python
import qa
run = qa.Run("my_script.py")
run.failure("PAGE_UNREACHABLE", "timeout", key=url, page_url=url, wba_id=wba, stage="monitor")
run.finish("ok", rows_in=n, rows_out=m, counters={"changed": c})
```

Writes only with `QA_COMMIT=true` and `AIRTABLE_QA_TOKEN` set; never raises into the host script (except under `QA_STRICT=true`, which the tests use).

## Corrections
`report_library.robot_type`, `robot_year`, `robot_title` hold what the pipeline settled on the first time `qa_sync.py` saw a value. When `source_type_check` or `curated_doc_title` later differs, that is a person's correction: it becomes a LABEL_CORRECTION failure and a `labels` row with `labelled_by = correction`. Never edit the robot_* fields by hand.

## The review loop
- **Daily sample** (`qa_sample.py`, to build): 15 cases a day into Today's cases: 5 high-stakes disagreements, 3 non-English report-shaped rows, 3 agreement controls (shown with no suggestion, to keep the accuracy measurement honest), 2 new arrivals, 2 unnamed cases.
- **Daily Claude review** (Sonnet): reads each case's cover and first pages, the fiscal-year evidence and the address; writes a suggested value and a one-line reason for type, year and title; notes recurring patterns. The owner adjudicates in Today's cases instead of classifying from scratch.
- **Weekly Claude review** (Opus): reads open failures, the week's labels and evals; turns patterns into pull requests (each with a regression test); answers the owner's `scan_concern` notes; writes what it did to `review_note` and to the changelog.

## The ship rule
A change to a rule or a prompt merges only when, on the evaluation labels, overall accuracy does not fall, accuracy on the classes that move a completeness check does not fall, and no regression case flips from right to wrong. `eval.py` records the verdict with the commit in `evals`.

## Snapshot: first picture, 22 Oct 2026
Dry run over 36,525 pages, 275,969 documents and 2,000 companies: 36,233 distinct open failures. TYPE_DISAGREE 12,163 (29,298 high-stakes disagreements in total, 17,135 of them on pre-2023 documents, counted only), TYPE_MISSING 4,705 (30,605 untyped non-English rows in total), PAGE_UNREACHABLE 4,105, CO_GAP 3,821, DOC_FETCH_FAIL 3,706, PAGE_EMPTY 3,093, DOC_UNREADABLE 1,496, PAGE_GONE 1,351, DOC_DUPLICATE 1,090, PAGE_BLOCKED 299 domains, DOC_WRONG_ROW 225, PAGE_UNHARVESTED 179. 266,726 rows awaited a robot_* snapshot.
