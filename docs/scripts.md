# Scripts and workflows

Every script is a dry run unless given `--commit` (scheduled and chained runs pass it). Times are UTC. "After X" means the workflow starts when workflow X completes successfully.

Known silent failures per script were inventoried on 21 Oct 2026 (commit aa278c6); the full table is in the design document linked from [quality.md](quality.md). The ones still open are tracked as rows in the Pipeline QA base.

## Nightly chain

| Script | Workflow, trigger | What it does | Writes |
| --- | --- | --- | --- |
| `airtable_monitor.py` | Monitor links (daily), 02:23 | Reads pages due today with plain requests; diffs document links; hands JavaScript pages to the browser lane | `monitored_links`: status, http_status, last_checked, seen_links, new_links, last_change, alert_status, page_language, needs_browser |
| `playwright_monitor.py` | Browser monitor, 00:03 | Renders the browser lane in Chromium, oldest first, 4,000 pages, 180-minute budget | same fields plus final_url |
| `monitor_core.py` | library | Which links count as documents (`doc_links`, `DOC_ENDPOINTS`, `DAM_SUFFIXES`, framework hosts), the diff, Airtable retries | |
| `harvest_reports.py` | Harvest report PDFs, after Monitor | seen_links of report-type pages into documents; doc_id and tombstone guards; shared-document guard (5+ companies); page health | `report_library` rows; `unharvested_links`, `unharvested_sample` |
| `doc_probe.py` | inside harvest (`--probe`) | Opens links that do not look like files; resolves viewer pages (Widen) | probe cache |
| `classify_library.py` | Classify report library, after Harvest; `--from-file` in Dedupe | Type from the address; then title, type and year from file name and cover | source_type, match_confidence, auto_title, doc_year |
| `attach_files.py` | Attach report PDFs, after Classify | Asks Airtable to fetch each document's file | file |
| `recover_attach.py` | Recover missing attachments, 19:00 | Downloads what Airtable could not, uploads it (5 MB limit), records why not | file, file_status (blocked, gone, too_big, not_pdf) |
| `build_cover_text.py` | Build cover text, after Attach or Recover | First six pages: text, language, page count, SHA-256, size | cover_text, cover_status, page_count, doc_language, content_hash, file_size_mb |
| `dedupe_library.py` | Dedupe report library, after Cover text | Discards duplicates (same stripped URL, same size and pages and name, same hash) and non-disclosures (SEC insider forms, call transcripts) | discard, discard_reason; `dedupe_log.csv` artifact |
| `date_documents.py` | inside Dedupe (30,000 a night); Document years (rules), manual, whole backlog | doc_year from file name, cover, filing or address | doc_year, doc_year_from |
| `doc_scope.py` | Document scope, after Cover text; manual | Whole report, part of a report, or summary or short document, by rules (file name, title, page count, first pages); report families only | scope, scope_reason |
| `company_coverage.py` | Company coverage, after Cover text; per company from scan_now and inbox | Completeness, gaps, overdue reports, cadence, work queue, the newest document received and how it came | 24 fields on `companies` |
| `base_health.py` | Base health, after Coverage and 12:17 | The day's health row, including Pipeline QA size, open failures and reviewed labels | `base_health` |
| `qa_sync.py` | QA sync, after Coverage | Status fields into failure rows (including analysts' `analyst_flag` as DOC_FLAGGED); robot_* snapshots (40,000 a night); corrections into labels; closes failures silent 30 days | Pipeline QA `runs`, `failures`, `labels`; `report_library` robot_type, robot_year, robot_title |
| `qa_sample.py` | QA sample, weekdays 04:31 | The day's ~15 review cards: 5 high-stakes disagreements (least-labelled pair first), 3 non-English, 3 blind agreement controls, 2 new arrivals, 2 reserved; housekeeping of reviewed and stale cards | Pipeline QA `labels` |
| `qa_archive.py` | QA archive, monthly on the 1st | Closed failures last seen 90+ days ago: archived to the private text-store repository (release qa-archive), verified, then deleted from Pipeline QA | Pipeline QA `failures` (deletes); archive file |
| `sl_labels.py` | Source Library labels, manual (dry run unless mode=commit) | Matches Source Library records to library rows (same company and file); writes analyst type and year as labels, marks doubtful ones not sure; scores URL type, AI type, the live rule and the year formula | Pipeline QA `labels` (stratum source_library), `evals` (baseline) |
| `qa.py` | library | The quality log: `Run`, `failure()`, `label()`, `finish()`, `close_silent()` | Pipeline QA |

## Full text

| Script | Workflow, trigger | What it does | Writes |
| --- | --- | --- | --- |
| `build_fulltext.py`, `text_core.py` | Build full text, 21:37 | Every page of every attached report (2021+) into the text store, sharded | text store |
| `fiscal_year.py`, `fiscal_years.py` | Fiscal years, after Full text | Reporting year with score and evidence: from the full text when the document is in the text store, else from its first pages (`cover_text`, up to 60,000 a run); verifies the AI year's quote | fiscal_year, period_end, year_score, year_basis, year_evidence, ai_year_verified |
| `sustainability_class.py` | Sustainability class, after Full text; manual | Where the sustainability disclosure lives (D dedicated, E embedded, S specialized, F financial only) by type and by the pages; the standards named (ESRS, GRI, SASB, ISSB, TCFD, TNFD, SDGs...); external assurance of the sustainability information; from the full text, else the first pages (up to 60,000 a run) | sustainability_class, class_reason, sustainability_share, frameworks, assurance |
| `mention_search.py` | Mention search, after Full text; on demand | Saved "which companies mention X" searches | `mention_searches`, `mention_hits` |
| `key_figures.py` | Key figures, Monday 09:20 | Revenue and employees from SEC and ESEF data, else a candidate from the latest report | revenue, employees and their sources on `companies` |

## On demand and weekly

| Script | Workflow, trigger | What it does | Writes |
| --- | --- | --- | --- |
| `scan_now.py` | Scan now, every 30 minutes | Runs the whole chain for ticked or new pages; runs `inbox.py` | scan_status, scan_outcome, last_manual_scan |
| `inbox.py` | inside Scan now | add_link, add_file, approve_suggestions, delete_now, new mention searches, live coverage | add_result; new pages and documents; tombstones in `state/` |
| `discover_pages.py` | Suggest missing report pages, 04:40 | Crawls the website of companies missing a hub or sustainability page | suggested_links, suggestions_checked |
| `regulator_filings.py` | Regulator filings, Monday 06:20 | SEC 10-K / 20-F and ESEF annual filings for 2023+ gaps | documents typed Annual Report; sec_cik, filings_note |
| `capture_pages.py` | Capture web pages, Sunday 00:23 | Prints pages ticked capture_page to PDF | `webpage_library` |
| `import_source_library.py` | Import Source Library, manual | Analysts' reports 2023+ and missing policies, with duplicate guards; reads Source Library read-only | documents (uploads) |
| `link_companies.py`, `assign_analysts.py` | Link and assign, 07:03 | Company links and analyst on pages and documents | company, companies, assigned_analyst |
| `assign_ids.py` | Assign join ids, 02:03 | link_id on pages | link_id |
| `purge_discarded.py` | Purge discarded rows, manual, two-step | Deletes rows discarded 30+ days, archived and tombstoned first | deletions; tombstones |

## Manual, one-off and diagnostic
`active_search.py` (homepage crawl for new pages), `link_documents.py` (one-off source_page links), `fetch_logos.py`, `unmonitor_shared.py` (switch off pages shared by 3+ companies), `diagnose_attach.py`, `diagnose_blocked.py` (read-only diagnoses), `ids.py` (normalize, doc_key, doc_id, link_id).

## Tests
`tests/` holds offline tests (no network, fake Airtable). The Tests workflow runs them on every pull request and every push to `main`: `QA_STRICT=true python -m unittest discover -s tests -v`.
