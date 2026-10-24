# Pipeline QA

The pipeline runs unattended and nobody reviews the 275,000 documents by hand, so its accuracy has to be measured, not assumed. Pipeline QA does three things: it records every failure the pipeline can name (one row per distinct failure, not per occurrence), it builds a labelled truth set a little every day, and it scores every change to a rule or a prompt against that truth set before it is merged.

Design document (21 Oct 2026, inventory of all 34 scripts at the time, the reasoning behind each choice): https://claude.ai/artifact/Qkz8e2Sn8PWtooz1YStPXf

## The base: Pipeline QA (`appJQ2mRtjHjzLq9E`)

| Table | One row per | Written by |
| --- | --- | --- |
| `runs` | script execution (`run_key` = workflow + GitHub run id) | `qa.Run.finish()` |
| `failures` | distinct failure (`failure_key` = sha1 of code + key); `occurrences`, `last_seen`, `last_commit` move on each recurrence | `qa.Run.failure()`, `qa_sync.py` |
| `labels` | one judged fact about one document, page or company (the golden set) | the daily sampler and review, corrections, Source Library, audits; the owner's answers on the cards |
| `evals` | one scoring of one rule or prompt against the labels | `sl_labels.py` (baseline scores); pull request measurements are posted as PR comments by `year_eval.py` and the coverage comparison |

Interface "Pipeline QA": **Check Claude's answers** (the day's cards left for the owner), **Open failures** (by code, severity, stage), **Runs**.

Every field carries a description in Airtable. Rules: a failure key that does not recur for 30 days is closed as `resolved_silently` with the last commit seen; a failure marked `fixed` that recurs reopens itself with a note; a label a person wrote is never overwritten.

## Size and archive
Pipeline QA has its own 500,000-record cap. Failures are keyed, so a recurring failure updates its row instead of adding one; growth comes from new failures (a few hundred a day) and labels (about 15 a day). `base_health` reports `qa_records_total` and `qa_base_full` daily and turns Check at 80%. On the 1st of each month `qa_archive.py` moves closed failures (fixed, resolved_silently, wontfix) last seen 90+ days ago into a gzipped JSONL file in the private text-store repository (release `qa-archive`), verifies the upload, then deletes them from the base. Labels, evals, runs and open or expected failures are never archived.

## Who acts on a failure
Nothing depends on a person working through failure rows: there are tens of thousands of them, one owner, and analysts who may or may not review. Every code has an automatic owner, a script or a scheduled Claude task. The owner acts only on the at most 5 items a week the Monday notification lists ("Para ti esta semana"); analyst flags, when they come, are checked by the weekly review first. `who_fixes` says it on every row and `open_in_library` opens the page, document or company in the Disclosure Library. Fixes are always made in the Disclosure Library or in the code; Pipeline QA is never edited by hand except labels, status and review notes. The next qa_sync sees the fix and closes the row.

| Code | Automatic owner | Built? |
| --- | --- | --- |
| PAGE_UNREACHABLE | the monitors retry every night; pages that need a browser move to the browser lane | yes |
| PAGE_GONE | nightly (Suggest missing report pages): searches the company's site, adds a clear replacement automatically and stops monitoring the gone page; the weekly review handles what stays ambiguous | yes (#54) |
| PAGE_BLOCKED, DOC_FETCH_FAIL (blocked) | the office-network runner on the owner's computer, every 2 hours (pc_fetch.py, [pc_runner.md](pc_runner.md)): refused files and refused report pages | yes (#60, once set up) |
| PAGE_EMPTY, PAGE_UNHARVESTED, DOC_DROPPED | the weekly review fixes the collection rules in code (one fix closes every page on the same platform) | yes |
| PAGE_WRONG_TYPE | the weekly review fixes the page type | weekly review only (needs the task update) |
| DOC_FETCH_FAIL (too_big) | read from the document's link | yes (#47) |
| DOC_UNREADABLE | scanned PDFs: first pages read by text recognition every day (OCR scanned covers); broken files stay counted | yes (#57) |
| DOC_DUPLICATE, DOC_WRONG_ROW, LABEL_CORRECTION | nobody: they record something already done | |
| TYPE_DISAGREE | rule fixes measured against the Source Library labels and the daily cards | yes |
| TYPE_MISSING (non-English) | cover-title phrases in other languages (cover_terms.json), nightly in Dedupe, measured on every change | yes (#55) |
| CO_GAP | the daily gap research (25 companies a day) | yes |
| CO_MISSED_KNOWN | Source Library matching, the daily research | partly |
| RUN_FAILED | a failed scheduled or chained job re-runs itself once (Re-run failed job once); a second failure is listed by base_health and read by the weekly review | yes (#52) |
| DOC_FLAGGED | the weekly review checks each flag and fixes the row when the flag is clearly right; the owner only when it lists it. A bonus signal: analysts may or may not flag, and nothing depends on it | yes |

A code whose owner is "to build" is a gap in the design, not a job for a person: it stays open in Pipeline QA (counted, so its size is known) until its automatic owner exists.

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
| DOC_FLAGGED | an analyst flagged a document in the Analysts interface (wrong_type, wrong_year, wrong_company, part_of_report, not_disclosure, duplicate, file_broken, other) | report_library.analyst_flag |
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
- **Analyst flags** (Analysts interface in the Disclosure Library; a bonus signal, nothing depends on it): analysts who use the reports may flag a document with `analyst_flag` (and say what they saw in `analyst_flag_note`), or describe a page problem in `scan_concern`. A flag changes nothing by itself. `qa_sync.py` turns each flag into a DOC_FLAGGED failure (High when a report-shaped document is flagged for type, year, company, scope or not being a disclosure). The owner fixes the document with the usual tools (type, `curated_doc_title`, discard) and clears the flag; the fix becomes a correction label whose reason records the flag, and the failure closes. The interface's Flagged documents page lists every open flag.
- **Daily sample** (`qa_sample.py`, weekdays 04:31 UTC): one card per document judging type, year and title together; 15 cards a day: 5 high-stakes disagreements, 3 non-English report-shaped rows, 3 agreement controls (the pipeline's type kept in `hidden_values`, never shown, to keep the measurement honest), 2 new arrivals, 2 unnamed cases (extra disagreements while none exist). A document already labelled is never picked again.
- **Daily labelling by Claude** (Opus, weekdays 12:46 Madrid; changed from a Sonnet pre-read on 23 Oct 2026): reads each card's document and writes the answer (`suggested_value`, `suggested_year`, `suggested_title`), `claude_confidence` and `claude_evidence` (quote, page, type-guide rule); blind controls are read without the pipeline's values. It keeps for the owner only the cards that need a person (low or medium confidence; high stakes, i.e. its type differs from the pipeline's and either moves a check; 2 high-confidence audit cards a day), about 5 and at most 8, with `check_reason`; it unticks `in_review` on the rest.
- **The owner's check** (interface page Check Claude's answers): one click in `your_call` (Agree, Disagree with only the wrong value corrected, Can't tell).
- **Settling** (`qa_sample.py` housekeeping, `settle()`): Agree takes Claude's answer as the truth; Disagree keeps the owner's correction and fills the rest from Claude's answer; Can't tell marks it not sure; all three `labelled_by` = Edi. Cards Claude left out, and cards unanswered for 3 days, become Claude's labels (`labelled_by` = Claude, sure only at high confidence). A value a person wrote is never overwritten. The old way (filling `true_type` / `true_year` directly) still counts as the owner's.
- **What each label may score.** Owner-checked and Source Library labels score everything. Claude's labels score the address classifier and the year rules, never the Airtable AI type (a model graded by a model). The audit cards measure Claude's own accuracy.
- **Weekly Claude review** (Opus): reads open failures, the week's labels and evals; turns patterns into pull requests (each with a regression test); answers the owner's `scan_concern` notes; writes what it did to `review_note` and to the changelog.

## Source Library as ground truth
`sl_labels.py` matches every Source Library record to the Disclosure Library row holding the same file (same company and byte size, or same specific file name; rows imported from the Source Library are skipped) and writes the analyst's type and year as a label. The 2025 base was reviewed, so its labels count as sure; 2024 labels are written not sure. Analysts can still be wrong, so a label is also marked not sure when the pipeline has strong contrary evidence: a full-text year with score 80+ that differs, or URL and AI agreeing on a type that satisfies a different completeness check. Those doubtful cases are worth a person's look. The sure labels give the baseline evals: exact accuracy, and check-level accuracy (does the prediction satisfy the same completeness check as the truth).

Rule changes to the year (`fiscal_year.py`, `date_documents.py`) are gated by `year_eval.py`: on the pull request it recomputes the {year} formula for every document with a sure true year, once with the rules on main and once with the branch, and posts accuracy plus the documents that flip either way. Merge only when it fixes more than it breaks; run the bulk rewrite after the merge.

## The ship rule
A change to a rule or a prompt merges only when, on the evaluation labels, overall accuracy does not fall, accuracy on the classes that move a completeness check does not fall, and no regression case flips from right to wrong. Today the measurement is the pull request comment (`year_eval.py`, coverage comparison); a general `eval.py` that writes every scoring to `evals` is not built yet.

## Snapshot: first picture, 22 Oct 2026
Dry run over 36,525 pages, 275,969 documents and 2,000 companies: 36,233 distinct open failures. TYPE_DISAGREE 12,163 (29,298 high-stakes disagreements in total, 17,135 of them on pre-2023 documents, counted only), TYPE_MISSING 4,705 (30,605 untyped non-English rows in total), PAGE_UNREACHABLE 4,105, CO_GAP 3,821, DOC_FETCH_FAIL 3,706, PAGE_EMPTY 3,093, DOC_UNREADABLE 1,496, PAGE_GONE 1,351, DOC_DUPLICATE 1,090, PAGE_BLOCKED 299 domains, DOC_WRONG_ROW 225, PAGE_UNHARVESTED 179. 266,726 rows awaited a robot_* snapshot.
