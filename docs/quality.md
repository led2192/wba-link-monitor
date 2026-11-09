# Pipeline QA

The pipeline runs unattended and nobody reviews the 275,000 documents by hand, so its accuracy has to be measured, not assumed. Pipeline QA does three things: it records every failure the pipeline can name (one row per distinct failure, not per occurrence), it builds a labelled truth set a little every day, and it scores every change to a rule or a prompt against that truth set before it is merged.

Design document (21 Oct 2026, inventory of all 34 scripts at the time, the reasoning behind each choice): https://claude.ai/artifact/Qkz8e2Sn8PWtooz1YStPXf

## The base: Pipeline QA (`appJQ2mRtjHjzLq9E`)

| Table | One row per | Written by |
| --- | --- | --- |
| `runs` | script execution (`run_key` = workflow + GitHub run id, plus `.` and `QA_RUN_PART` when one run has several parts, such as the shards of Propose values) | `qa.Run.finish()` |
| `failures` | distinct failure (`failure_key` = sha1 of code + key); `occurrences`, `last_seen`, `last_commit` move on each recurrence | `qa.Run.failure()`, `qa_sync.py` |
| `labels` | one judged fact about one document, page or company (the golden set) | the daily sampler and review, corrections, Source Library, audits; the owner's answers on the cards |
| `evals` | one scoring of one rule, prompt or stage against known answers: the scoreboard. One row per target, version and run (`eval_id`), so the trend is the rows of one target over time | `sl_labels.py` (type and year against the Source Library labels), `extract_eval.py` (reader per attribute against the validated answers: right, and the number on the ranked pages), `narrative_eval.py` (judgement attributes against the ACT Core answers), `rank_eval.py` (page ranking methods against the cited pages: one row per method, accuracy = cited page in the top 3), `element_eval.py` (cited documents held, held by type and year, with text, a quote found), audits recorded by hand (web search precision); written by `qa.Run.evaluation()` on committed runs only. Pull request runs still post a comment and write nothing |
| `narrative_disagreements` | wrong answer of a manual narrative evaluation run (`key` = run id + Metadata ID): the analyst's answer and evidence next to the model's answer, quote and page, and where it went wrong; `verdict` and `note` are for the person judging | `narrative_eval.py --disagreements` (manual runs); the owner judges. Kept here because the answers are WBA assessments and the repository's logs and PR comments are public |

Interface "Pipeline QA": **Check Claude's answers** (the day's cards left for the owner), **Open failures** (by code, severity, stage), **Runs**.

Every field carries a description in Airtable. Rules: a failure key that does not recur for 30 days is closed as `resolved_silently` with the last commit seen; a failure marked `fixed` that recurs reopens itself with a note; a label a person wrote is never overwritten.

## Size and archive
Pipeline QA has its own 500,000-record cap. Failures are keyed, so a recurring failure updates its row instead of adding one; growth comes from new failures (a few hundred a day) and labels (about 15 a day). `base_health` reports `qa_records_total` and `qa_base_full` daily and turns Check at 80%. On the 1st of each month `qa_archive.py` moves closed failures (fixed, resolved_silently, wontfix) last seen 90+ days ago into a gzipped JSONL file in the private text-store repository (release `qa-archive`), verifies the upload, then deletes them from the base. Labels, evals, runs and open or expected failures are never archived.

## Who acts on a failure
Nothing depends on a person working through failure rows: there are tens of thousands of them, one owner, and analysts who may or may not review. Every code has an automatic owner, a script or a scheduled Claude task. The owner acts only on the at most 5 items a week the Monday notification lists ("Para ti esta semana"); analyst flags, when they come, are checked by the weekly review first. `who_fixes` says it on every row and `open_in_library` opens the page, document or company in the Disclosure Library. Fixes are always made in the Disclosure Library or in the code; Pipeline QA is never edited by hand except labels, status and review notes. The next qa_sync sees the fix and closes the row.

| Code | Automatic owner | Built? |
| --- | --- | --- |
| PAGE_UNREACHABLE | the monitors retry every night; pages that need a browser move to the browser lane | yes |
| PAGE_GONE | nightly (Suggest missing report pages): searches the company's site, adds a clear replacement automatically and stops monitoring the gone page; the weekly review handles what stays ambiguous. Gone web-report pages (type sustainability_report) had no owner until 2 Nov 2026 | yes (#54; sustainability_report pages since 2 Nov 2026) |
| PAGE_BLOCKED, DOC_FETCH_FAIL (blocked) | the office-network runner on the owner's computer, every 2 hours (pc_fetch.py, [pc_runner.md](pc_runner.md)): refused files and refused report pages | yes (#60, once set up) |
| PAGE_EMPTY, PAGE_UNHARVESTED, DOC_DROPPED | the weekly review fixes the collection rules in code (one fix closes every page on the same platform) | yes |
| PAGE_WRONG_TYPE | the weekly review fixes the page type and ticks scan_now (up to 20 High pages a week, with PAGE_GONE) | yes (weekly review, since 26 Oct 2026) |
| DOC_FETCH_FAIL (too_big) | read from the document's link | yes (#47) |
| DOC_UNREADABLE | scanned PDFs: first pages read by text recognition every day (OCR scanned covers); broken files stay counted | yes (#57) |
| DOC_DUPLICATE, DOC_WRONG_ROW, LABEL_CORRECTION | nobody: they record something already done | |
| TYPE_DISAGREE | rule fixes measured against the Source Library labels and the daily cards | yes |
| TYPE_MISSING (pending, English) | the Airtable AI types it; base_health turns Check when the AI typed under half of the English arrivals of 2 to 9 days ago (since 9 Nov 2026); the address, file name and cover title type what they can nightly (Dedupe) | yes (watch only: restarting the AI is the owner's) |
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
| DATA_CHECK | a table of the evidence database does not look the way it should (a map built for the wrong years, lines without embeddings, documents without pages, a stale value table, orphan versions) | evidence_health (daily; key = the check's name, so a recurring failure updates its row) |

Document rows are written only for report-shaped documents (a report-family type, or a report word plus a 2023+ year); the rest is counted in the run's counters. Severity is High when the failure hides or costs a 2023+ sustainability or annual report, or blocks a whole domain. For a gone or unreachable page that means a report hub, a sustainability report page, or a sustainability or investor page whose address lists reports (`qa_sync.page_holds_reports`, since 26 Oct 2026); a gone topic page is Medium.

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
- **Attribute values reviewed by analysts** (`review_score.py`, Mondays 07:52 UTC, since 31 Oct 2026): every `attribute_values` row an analyst accepted or rejected is a labelled answer. The weekly report (run artifact) gives, per method and attribute, the acceptance rate; the rows that matched the validated 2025 answer and were still rejected (both wrong, or the reviewer wants another line) and the rows that differed and were accepted (the validated answer was wrong); the rejection notes grouped into patterns (another line of the statement, the wrong year, a scale or unit, a segment, the validated answer itself wrong, the figure not found); the corrected values; the pace. The weekly Claude review reads it and turns the patterns into pull requests, the way the first review turned "net sales instead of sales" into the revenue top-line rule (#146).

## Source Library as ground truth
`sl_labels.py` matches every Source Library record to the Disclosure Library row holding the same file (same company and byte size, or same specific file name; rows imported from the Source Library are skipped) and writes the analyst's type and year as a label. The 2025 base was reviewed, so its labels count as sure; 2024 labels are written not sure. Analysts can still be wrong, so a label is also marked not sure when the pipeline has strong contrary evidence: a full-text year with score 80+ that differs, or URL and AI agreeing on a type that satisfies a different completeness check. Those doubtful cases are worth a person's look. The sure labels give the baseline evals: exact accuracy, and check-level accuracy (does the prediction satisfy the same completeness check as the truth).

Rule changes to the year (`fiscal_year.py`, `date_documents.py`) are gated by `year_eval.py`: on the pull request it recomputes the {year} formula for every document with a sure true year, once with the rules on main and once with the branch, and posts accuracy plus the documents that flip either way. Merge only when it fixes more than it breaks; run the bulk rewrite after the merge.

## The ship rule
A change to a rule or a prompt merges only when, on the evaluation labels, overall accuracy does not fall, accuracy on the classes that move a completeness check does not fall, and no regression case flips from right to wrong. The measurement on a pull request is still its comment (`year_eval.py`, coverage comparison, the reader and element evaluations); the manual and scheduled runs of the evaluators write their scores to `evals` (since 3 Nov 2026), which is the scoreboard to read for trends.

## Snapshot: week of 9 Nov 2026 (third weekly review)
Open failures: 46,764 distinct rows (44,393 a week ago). TYPE_DISAGREE 12,555, DOC_FETCH_FAIL 6,502, TYPE_MISSING 5,852, PAGE_UNREACHABLE 5,270, DOC_DUPLICATE 4,186, CO_GAP 3,623, PAGE_EMPTY 3,596, DOC_UNREADABLE 1,776, PAGE_GONE 1,421, DOC_WRONG_ROW 825, PAGE_BLOCKED 505 domains, LABEL_CORRECTION 430, PAGE_UNHARVESTED 217, DOC_FLAGGED 4 (flags cleared on 2 Nov; the rows close by themselves on 2 Dec), RUN_FAILED 2. Fixed 22 (21 gone pages by #170, the One hop charset crash by #220), expected 15. No failure marked fixed came back.

Opened 3 to 9 Nov: 2,393 rows, 861 High. The largest: DOC_DUPLICATE 769 (Low), TYPE_MISSING 569 High (417 "pending", 120 "no_cover", 32 "non_english"), PAGE_UNREACHABLE 336 (116 High: 67 no response, 34 with the stale status "200"), DOC_WRONG_ROW 291, DOC_FETCH_FAIL 174 (88 High: 32 too big, 31 refused, 15 download failures), PAGE_EMPTY 130, CO_GAP 45, PAGE_BLOCKED 30, PAGE_GONE 24 (9 High; the nightly gone-page search unticked all but today's one).

The week's main finding: the Airtable AI fields (`source_type_check`, `source_title`) stopped answering around 20 Oct. Of the English documents that arrived on 19 Oct 79% were typed; after 25 Oct about 2%. About 35,000 documents wait for an AI type and title, and the TYPE_MISSING "pending" rows are that backlog turning 7 days old. Nothing failed, so nothing said so; `base_health` now does (#302). Restarting the fields is the owner's (AI credits or automatic generation in Airtable).

Runs: 314 recorded since 2 Nov, 16 failed and 5 partial. The partial runs are One hop stopping at its time budget (by design) and one narrative evaluation; the failures are One hop on 5 Nov (charset, fixed by #220) and 15 manual Generic reader runs on the night of 8 to 9 Nov (the evidence database timing out under several runs at once, then the Anthropic credit running out). base_health was Problem every day (1 to 6 failed jobs a day); no row on 5 Nov. The Disclosure Library holds 419,200 records of 500,000 (83.8%, 377,530 on 1 Nov) with none ready to purge; the model calls cost about $50 in 7 days.

Labels so far: 9,217 (Source Library 8,622, corrections 430, daily cards: 94 settled by Claude, 32 by the owner, 39 waiting). This week's cards: 75, 36 settled. Where the address type and the AI type disagreed (14 settled), the AI was right 8 times, the address once, neither 5; no single pair has 10 sure labels, so no new proposal. Agreement controls: all 9 settled ones right. Year: wrong on 2 of 31 cards with a true year (94% right). Titles: wrong on 7 of 36 settled cards.

## Snapshot: week of 2 Nov 2026 (second weekly review)
Open failures: 44,393 distinct rows (39,048 a week ago). TYPE_DISAGREE 12,555, DOC_FETCH_FAIL 6,328, TYPE_MISSING 5,283, PAGE_UNREACHABLE 4,934, CO_GAP 3,578, PAGE_EMPTY 3,466, DOC_DUPLICATE 3,417, DOC_UNREADABLE 1,764, PAGE_GONE 1,418, DOC_WRONG_ROW 534, PAGE_BLOCKED 475 domains, LABEL_CORRECTION 428, PAGE_UNHARVESTED 209, DOC_FLAGGED 4 (all four checked and cleared on 2 Nov). Expected 15, fixed 1, none closed silently yet (the first silent closes come on 21 Nov).

Opened 27 Oct to 2 Nov: 4,943 rows, 1,959 of them High. The largest: DOC_DUPLICATE 1,800 (Low, a record of dedupe's work), DOC_FETCH_FAIL 1,036 (526 High refused files, for the office-network runner), TYPE_MISSING 361, LABEL_CORRECTION 358 (people correcting types, each one also a label), PAGE_UNREACHABLE 315, PAGE_EMPTY 240, TYPE_DISAGREE 234. No failure marked fixed came back.

What is still recurring is the part nothing fixes. Of 966 open High PAGE_GONE rows, 861 were last seen on or before 31 Oct (their page was unticked and they will close by themselves); the 105 seen every night were all pages of type `sustainability_report`, which the gone-page search skipped (fixed in #170). Of 4,934 PAGE_UNREACHABLE rows, 1,493 carry the detail "200": the browser lane failed on the page but the last good status stayed in `http_status`; 175 of them are file addresses (PDF, document downloads) watched as pages.

Runs: 221 recorded, 9 failed, all Search check runs of 26 to 28 Oct (timed-out test searches while the evidence database was being tuned), none since. base_health was Problem on 6 of the last 7 days (2 to 8 failed jobs a day: Propose values, readers and evaluations on 30 Oct, the first web search wave on 1 Nov, tests of pull request branches), OK on 31 Oct. The Disclosure Library holds 377,628 records of 500,000 (75.5%; base_health turns Check at 80%).

Labels so far: 8,765 (Source Library 8,247 of which 7,383 sure, corrections 428, daily cards 90). This week's cards: 75, 46 settled (by the owner or by Claude). Where the address type and the AI type disagreed (19 cards), the AI was right 11 times, the address 1, neither 7; no single pair has 10 sure labels yet, so no new proposal. Agreement controls: on 6 of 11 the type both agreed on was wrong, 5 of them in a way that moves a completeness check (Annual Report for a financial statement, Integrated Report for a statutory filing, Impact or Sustainability Report for an environmental or engagement report); too few to act on, watched next week. Year: wrong on 3 of 41 cards with a year (93% right). Titles: wrong on 20 of 46 settled cards, 9 of the 11 non-English ones.

## Snapshot: week of 26 Oct 2026 (first weekly review)
Open failures: 39,048 distinct rows (all first seen 22-26 Oct; none fixed or closed yet, the 30-day silent close starts on 21 Nov). TYPE_DISAGREE 12,318, DOC_FETCH_FAIL 5,271, TYPE_MISSING 4,921, PAGE_UNREACHABLE 4,519, CO_GAP 3,544, PAGE_EMPTY 3,185, DOC_UNREADABLE 1,558, DOC_DUPLICATE 1,471, PAGE_GONE 1,400 (17 more set expected by the review), PAGE_BLOCKED 351 domains, DOC_WRONG_ROW 260, PAGE_UNHARVESTED 181, LABEL_CORRECTION 69. High: 26,768. No DOC_FLAGGED and no PAGE_WRONG_TYPE row yet. Runs: 27 recorded, none failed; base_health was Problem on 22-24 Oct (a failed job each day, re-run) and OK on 25-26 Oct.

Labels so far: 8,247 Source Library labels (7,383 sure), 69 corrections, 15 daily cards (not yet checked by the owner). On the sure labels the year formula is right for 3,269 of the 3,957 documents with a year (83%). Where the address type and the AI type disagree, the AI (the pipeline's choice) is right on the large pairs (Integrated Report vs Sustainability Report: 513 of 540) and wrong on others; the pairs under 70% are listed in [decisions.md](decisions.md) (proposal of 26 Oct 2026). Titles: 2 of the 15 cards had a wrong title.

## Snapshot: first picture, 22 Oct 2026
Dry run over 36,525 pages, 275,969 documents and 2,000 companies: 36,233 distinct open failures. TYPE_DISAGREE 12,163 (29,298 high-stakes disagreements in total, 17,135 of them on pre-2023 documents, counted only), TYPE_MISSING 4,705 (30,605 untyped non-English rows in total), PAGE_UNREACHABLE 4,105, CO_GAP 3,821, DOC_FETCH_FAIL 3,706, PAGE_EMPTY 3,093, DOC_UNREADABLE 1,496, PAGE_GONE 1,351, DOC_DUPLICATE 1,090, PAGE_BLOCKED 299 domains, DOC_WRONG_ROW 225, PAGE_UNHARVESTED 179. 266,726 rows awaited a robot_* snapshot.

## attribute_guidance (7 Nov 2026)
Table `attribute_guidance` (tblzKjcxDZfgw6qNl) in Pipeline QA holds WBA's own assessment guidance per attribute: gmc, md_id, question, guidance, md_prompt, units, year_rule, source. The first five rows (revenue, Scope 1, Scope 2 location and market, low-carbon capex) were copied on 7 Nov 2026 from the MD Guidance table of the LLM Assessment - ACT Test base, without its links to internal channels. `extract_eval --guidance md` reads it at run time. It is methodology, so it lives here and never in the repository, its logs or its artifacts.

## Checks on the measure itself (7 Nov 2026)
A gain is only real if the way it is measured cannot produce it. Every experiment of the readers keeps to these:
- Same answers in both arms. Two reader runs cut by a time budget at different points do not compare; give the budget to read the whole queue, or compare only the answers both read.
- No answer in its own training. Anything learned from the validated answers (page choice, pack words, choice weights) is fitted on four company folds (rank_eval.fold_of) and scored on the fifth. Words seen in fewer than 5 answers are left out, so no company's own words carry over.
- A control that must fail. `chooser_eval.py` also trains on answers whose right candidate was moved at random; that model must score near chance on the real answers. If it does not, a feature leaks the answer.
- Ties never flatter. Where two candidates have the same score, the wrong one is counted.
- The noise floor. A language-model reader varies by about 3 points per attribute between two runs of the same 65 answers; the rule of 3 points is measured against that, and a model run is repeated when a result is close. Measures without a model (chooser_eval, select_eval, pack_eval without --read) give the same numbers on a rerun.
- Conformal sets in `chooser_eval.py` are cross-conformal: the scores of the other folds calibrate the threshold, and those scores come from models that saw the test fold in training, so the stated coverage is close to, not exactly, the guarantee of split conformal prediction. A production calibration uses a held-out set.
- Paired statistics (7 Nov 2026, after the owner's notes on test design). Two options scored on the same answers are compared answer by answer, not by their two shares: the answers fixed and broken by the new option, the exact McNemar p on those, and a 95% interval of the difference that resamples companies rather than answers, because answers of one company are not independent (`paired_stats.py`; in `chooser_eval.py` for every option against the one before it and for Scope 2 chosen together; `compare_runs.py` for two Generic reader runs, which print per-answer outcomes since this date). A difference whose interval holds zero is "not distinguishable from chance" on that sample, whatever the 3-point rule says; the broken answers are read before an option is adopted.
- Stage gates. Each loss is placed at a stage before anything is tuned: figure in the year's documents (the ceiling of everything after), on the 25 ranked pages, on the model's 8 pages, then right. `extract_eval --select oracle` gives the model the pages that print the analyst's figure first: right in that run is the reader's own ceiling, so the gap to a normal run is what finding the pages costs and the gap to 100% is what reading costs. It uses the answer to choose pages and is never an option. The report also gives right among the answers whose figure is in the documents.
- Position bias. `extract_eval --order reversed` gives the model the same pages in the opposite order; compared with a normal run by `compare_runs.py`, more change than the run-to-run noise means the reader depends on where a page sits.
- Pictures. `visual_eval.py` checks how many figures missing from the text are printed in pictures, with a control that measures the visual reader on figures that are in the text.
- What our answers cannot test yet: the validated answers hold only disclosed figures, so a reader that always answers is never punished for answering where the company disclosed nothing. Not-disclosed answers (the analyst's "not found" with a reason) are the next set to add before any option that makes the reader answer more often is adopted.
- Held-out attributes (8 Nov 2026). Until then every option was chosen on the same 8 number and 5 narrative attributes it was measured on, and some choices were tuned on them by hand. `heldout.py` holds out a quarter of all attributes by a hash of the code (never an attribute already tuned on). Their specs are built at run time from Metadata Codes, with no reading notes or exclusions, so they measure what the readers do on an attribute nobody prepared, as the 2026 attributes will be. Nothing is tuned on them and their wrong answers are not read for tuning; an option is adopted only if neither the development attributes nor the held-out ones (by kind: numbers, yes/no and lists) get worse by more than 3 points (`extract_eval --heldout 20`, `narrative_eval --heldout 15`).

Still to build for the measure (agreed 7 Nov 2026, in this order):
1. Not-disclosed cases in the test set: answers where the analyst found the figure not disclosed, about 20% to 25% of the set, where the right reply is no figure. Scored as right abstentions and wrong answers.
2. Evidence-anchored answers: for each validated answer, the document, page and printed words it came from (the Source Library citations give part of it), so a miss can be placed at a page and not only at a document.
3. Agreement between analysts on a sample (Cohen's kappa or Krippendorff's alpha per attribute). Below 0.80 the attribute's definition is the problem, not the reader.
4. A test set stratified by how documents fail: borderless tables, headers nested on several levels, tables split across two pages, merged cells, footnotes that change a figure's boundary, scans and tables saved as images.
5. Per-gate measures: table cells read in the right row and column (Gate 1, column shifts), page recall at 1, 3, 5 and 10 and the reciprocal rank (Gate 2), table, unit and footnote together in what the model reads (Gate 3).
6. Checks that need no answers, on unvalidated documents: accounting constraints (parts that should add up to a total, Scope 1 plus 2 against a stated total, off by more than 1%) and the same figure read the same at another scale.
