# Changelog

Newest first. One line per change, with the pull request. Every pull request adds its line here.

## October 2026
- 2026-10-26 Docs, first weekly review: quality.md snapshot of the week (39,048 open failures, 8,331 labels, year formula right on 83% of sure labels); decisions.md proposal for the type pairs where the AI type is right less than 70% of the time (for the owner to approve, not applied); PAGE_WRONG_TYPE now owned by the weekly review (#PR)
- 2026-10-25 Attachments that are a web page instead of the PDF (Airtable stored a site's home page under the PDF's name, as on 79 ADB documents) lose the wrong attachment and go to the office-network runner; new `fix_wrong_attachments.py`, nightly after Build cover text (#73)
- 2026-10-24 Full text of every disclosure type (the nightly build now passes `--types all`: interim reports, statutory documents, presentations and certifications join, read over the following nights); evidence passages gain a financial group (revenue, EBITDA, capex, subsidiaries, employees) and cover every report type from 2024 (#72)
- 2026-10-24 OCR of scanned PDFs: up to 8,000 a night instead of 1,000; the first manual run after the fixes read 595 of 600 in 20 minutes, so the backlog of about 12,500 clears in about two nights (#71)
- 2026-10-24 Office-network runner: one job repeats the fetch every 2 hours for 22 hours (`--repeat-hours`, `--every-minutes`), because GitHub skipped two of the four scheduled runs that day; the first full day reached 231 refused files and read 79 refused pages (#70)
- 2026-10-24 Evidence passages: 2024+ key reports are cut into passages tagged climate, nature or social, by planning, implementation and performance and by integration aspect, following the Integrated Transition Assessment white paper (Table D.1); passages go to the private text store, `evidence_profile` and `evidence_version` to report_library; new `evidence_chunks.py` and `evidence_terms.json`, nightly after Full text (#69)
- 2026-10-24 Publication dates of key reports (2023+): regulator filing date, the report's own statement, board approval, or first seen by the robot, with the deciding sentence; new `pub_dates.py` after Fiscal years and report_library `pub_date`, `pub_date_precision`, `pub_date_source`, `pub_date_evidence` (#68)
- 2026-10-24 Key reports: for every company, the long annual report(s) with full-year financial and sustainability data for the 2026 and 2025 publication waves, with a subsidiary check on the title and cover; new `key_reports.py` after Company coverage, companies `kr_*` fields, report_library `entity_check` and `entity_name`, four counts on base_health. First measurement: 2026 wave complete for 855 companies, partial for 530, only 2025 for 353, neither for 262; 263 subsidiary reports left out (#67)
- 2026-10-24 More runs where work is queued: the browser monitor also runs at 12:03 UTC; OCR of scanned PDFs runs daily (first 4 pages, 4 workers, 290-minute budget) instead of monthly, and re-reads attachment links just before downloading them, because on 23 Oct 492 of 600 downloads failed, most likely on links that had expired; download failures are now split into attachments and links. The office-network runner moves to every 2 hours in the private repository (#66)
- 2026-10-24 Office-network runner: the last 25 minutes of each 90-minute run are kept for refused report pages (`--page-minutes`); in the first scheduled run (24 Oct, 04:41 UTC) the 300 files used the whole run, reaching 109 (62 attached, 47 read on the computer, 17 only through Edge), and no page was read (#65)
- 2026-10-24 Documentation catch-up: new `docs/diagrams.md` (11 diagrams: map, life of a document, the night, type and year ladders, the two completeness scores, the quality loop, the daily cards, refused files, who acts on each failure, the research), the nightly chain in `architecture.md` now shows the repair jobs and the office-network runner, the guide's timetable and Health page are current, and `fields.md` is regenerated (#64)
- 2026-10-23 The daily research pastes into `research_link` instead of `add_link`: same handling, but documents and pages it brings are marked as found by research (`latest_doc` says so), its gaps on the day are kept in `research_gaps_before`, and it skips documents the library already holds. First measurement (100 companies, 21-23 Oct): 94 documents and 10 pages added, about a third of its links already held (#63)
- 2026-10-23 Scanned PDFs (no text in the file) get their first pages read by text recognition once a month (new `ocr_covers.py`); `cover_ocr` marks such rows; covers that still give no text become `no_text_ocr` and are not retried (#57)
- 2026-10-23 Office-network runner, second pass: files and pages a plain request could not reach are tried again in Microsoft Edge without a window (up to 100 files a run), skipping paths robots.txt disallows and never solving captchas or logging in; the first test from the owner's computer reached 11 of 30 refused recent reports with plain requests (#62)
- 2026-10-23 `doc_label` showed #ERROR! on rows whose AI title was never generated (the new `ai_title` formula failed on an empty AI field); `ai_title` now guards every case and `doc_label` was checked on the rows that failed
- 2026-10-23 Office-network runner: `pc_fetch.py` fetches files that sites refuse to cloud servers from the owner's computer (self-hosted runner on the private repository wba-text-store only, weekdays); set-up guide in docs/pc_runner.md. The cloud retry (#53) was closed: 0 of 61 recovered (#60)
- 2026-10-23 Two completeness scores beside the combined one: `disclosure_completeness` (sustainability report, annual report, Code of Conduct, Human Rights Policy) and `page_completeness` (report hub, sustainability page) on companies, and their averages on base_health; the rules comparison shows both (#58)
- 2026-10-23 Completeness rules v3, approved and switched on: the report hub and sustainability page checks count only pages still watched that have not gone (404/410); `--compare` takes any two rule versions (`--compare-rules v2,v3`) and lists checks gained and lost (#58)
- 2026-10-23 Address classifier: prospectuses, base prospectuses, final terms, pricing supplements and offering circulars are Statutory Documents, not Registration Document (bond documents filed next to universal registration documents) (#56)
- 2026-10-23 A formula in an error state no longer crashes a script: Airtable reads now turn formula errors into empty values (`monitor_core.airtable_request`); `ai_title` guards against AI fields in an error state. Company coverage crashed on it in the PR comparison the same evening (#59)
- 2026-10-23 Documents with no type at all (mostly non-English: the AI types English only) are typed nightly from their file name and cover title, including cover-title phrases in a dozen languages (`cover_terms.json`); `year_eval.py` measures it on non-English documents with a sure true type (#55)
- 2026-10-23 Gone report pages (404/410) are replaced nightly: the company's site is searched and a clear best match is added automatically, the gone page unticked with a dated note; clear matches for missing hubs and sustainability pages are added without waiting for approval (#54)
- 2026-10-23 A scheduled or chained job that fails on its first attempt re-runs its failed jobs once, two minutes later (new workflow Re-run failed job once); RUN_FAILED now has an automatic owner (#52)
- 2026-10-23 AI titles that are sentences instead of titles ("I don't have access to the document text...", over 100 rows) no longer show: new formula `report_library.ai_title`, used by `doc_label` (live in Airtable) and by the cards' `title_value` (#51)
- 2026-10-23 Daily cards: Claude (Opus) labels every card with confidence and quoted evidence and leaves about 5 for the owner; `your_call` (Agree / Disagree / Can't tell) replaces classifying from scratch; `qa_sample.py` settles cards into labels (`settle()`), Claude's answer for cards it left out or unanswered for 3 days; interface page "Check Claude's answers" replaces Today's cases; new labels fields `your_call`, `claude_confidence`, `claude_evidence`, `check_reason` (#51)
- 2026-10-23 Docs: owner's guide (`docs/guide.md`) and field reference (`docs/fields.md`, generated by the new read-only `field_docs.py`); Pipeline QA failures are no longer a to-do list for the owner (`who_fixes` rewritten: the weekly review fixes gone and wrongly typed pages, the owner gets at most 5 items a week); card instructions: `true_type` and `true_year` always, `verdict` optional; stale `eval.py` references corrected (#50)
- 2026-10-23 Completeness rules v2 (approved): universal registration documents count as annual reports; whole annual reports with an embedded sustainability section (8+ pages or a named sustainability statement) count for the sustainability check; `sust_reporting` on companies; `--compare` and the Coverage rules comparison workflow measure rule changes; Document scope covers registration documents (#49)
- 2026-10-23 Blocked sites probe: read-only measure of which free route (full browser request, headless Chromium, Internet Archive, stock-exchange filings) reaches documents and pages whose site refuses our robot (#46)
- 2026-10-23 Address classifier: human rights policies named with modern slavery or as statements stay Human Rights Policy (3 of the 20 cases #45 broke); "sec form" no longer means 10-K, Philippine 17-C and US 8-K current reports are Statutory Documents; house rules: modern slavery statements are Social Reports, human rights statements are Human Rights Policy (classifier and Source Library labels) (#48)
- 2026-10-23 add_link opens a PDF before adding it and says when the site refuses it; add_result gives the first type and year guess; a PDF dropped with the link of a file-less row goes onto that row. Address classifier: shared tokens weigh less (ties no longer go to the first type listed), 10-Q and Indian integrated filings are interim reports, 'form 10' no longer means 10-K. year_eval.py also scores the address classifier (#45)
- 2026-10-23 Files too big to attach (over 5 MB, file_status too_big: 2,996 rows, mostly full annual and integrated reports) are read from document_url for first pages and full text, so they get a type and a year (#47)
- 2026-10-23 Year fixes after the 22 Oct rewrites: comparison periods ignored, title two years away from the text year lowers the score, ten-year tables are not cover titles; `year_eval.py` scores rule changes on every PR; `check_docs` and `completeness_change` say which document a lost check depended on; CO_GAP messages per check (#43)
- 2026-10-23 Sustainability class gaps: interim reports, statutory documents and presentations decided from their first pages; rows with no text classed by type; 10-Ks, registration documents and financial statements added to the full-text build; no cap on first-pages reads (#44)
- 2026-10-23 Companies get `latest_doc_added` (when the newest document with a file arrived, however it came) and `latest_doc` (year, title, type, how it arrived, library_id), written by company_coverage.py (#42)
- 2026-10-23 Analyst flags: `analyst_flag` and `analyst_flag_note` on report_library (set from the new Analysts interface) become DOC_FLAGGED failures in Pipeline QA; a fix of a flagged row becomes a correction label that records the flag (#41)
- 2026-10-22 Sustainability class: `sustainability_class.py` and its workflow write sustainability_class (D dedicated / E embedded / S specialized / F financial only), class_reason, sustainability_share, frameworks and assurance on report_library, by rules from the text in nine languages (#40)
- 2026-10-22 QA archive (monthly, closed failures 90+ days to the private store) and Pipeline QA size in base_health; who_fixes and open_in_library on failures (#37)
- 2026-10-22 Document scope by rules instead of the AI field (`scope`, `scope_reason`); manual workflow to run the doc_year rules over the whole backlog (#38)
- 2026-10-22 Fiscal years: documents without full text are read from their first pages (cover_text); first full run read 41,697 documents (#36)
- 2026-10-22 Source Library as ground truth: `sl_labels.py` writes analyst type and year as labels for every document both hold, flags doubtful ones, and scores the classifiers and the year rule (#35)
- 2026-10-22 QA sample: the daily review cards (type, year and title on one card; blind controls), weekdays 04:31 UTC (#34)
- 2026-10-22 Documentation: `CLAUDE.md` and `docs/` (architecture, scripts, quality, runbook, decisions, pitfalls, this changelog); documentation duty for every PR (#33)
- 2026-10-22 Pipeline QA: `qa.py` quality log, `qa_sync.py` nightly sync of status fields into failure rows, robot_* snapshots and correction capture, offline tests and the Tests workflow (#32)
- 2026-10-22 One year to use: more reader phrasings; the AI year counts only when its quote is found in the document (#31)
- 2026-10-21 Fiscal year read from the full text, with a confidence score and the evidence sentence (#30)
- 2026-10-21 Import Source Library 2025/2024 reports and missing policies, with duplicate guards (#29); run on 21 Oct: about 1,860 documents added
- 2026-10-21 doc_year from the file name and cover page, not the upload folder (#28)
- 2026-10-21 Fix: harvest wrote one page twice in a batch and failed at the last step (#27)
- 2026-10-21 file_size_mb and content_hash (SHA-256) on every document (#25, #26)
- 2026-10-21 add_link: a pasted page watched under a non-harvested type is retyped and scanned (#24)
- 2026-10-20 Key figures: revenue and employees per company with the exact source (#22, #23)
- 2026-10-20 Live coverage: a company is refreshed within minutes of any change (#21)
- 2026-10-20 Liferay document links; a dropped file is paired with the link the site refused (#20)
- 2026-10-20 Annual reports from SEC and ESEF for gaps (#18); refused pages count as blocked in My queue (#19)
- 2026-10-20 Auto titles and years for every read document; delete pages for good (#17)
- 2026-10-20 My queue, add_link, delete_now, suggested pages with approval (#15); new mention searches run at once (#16)
- 2026-10-20 Diagnose refused pages (#14); label documents from file name and title page (#13)
- 2026-10-20 Pages that refuse robots labelled blocked, not dead (#12); chopped links with spaces dropped (#11); Scan now keeps watching (#10)
- 2026-10-19 Manual-scan signals, pages missing documents, overdue reports (#8)
- 2026-10-19 PDFs behind viewer pages and asset platforms (Widen and others) (#6, #7); nightly probe of links that do not look like files (#5)
- 2026-10-19 Base health, completeness, safe purge, Scan now (#4)
- 2026-10-19 Discard non-disclosures: SEC insider filings, call transcripts (#3); stop duplicate documents, nightly dedupe (#2)
- 2026-10-19 Full-text store and mention search (#1)

## July 2026
- 2026-07-27 Extensionless document endpoints (Q4 static-files, Kentico); company logos; analyst assignment; sust_reports_total
- 2026-07-24 Link documents to their source page
- 2026-07-17 Cross-domain PDFs admitted by default (v3); shared-document guard; missing_types
- 2026-07-15 Browser monitor daily as a rotating queue; needs_browser
- 2026-07-14 Company coverage
- 2026-07-13 Airtable retries with escalating timeouts
- 2026-07-03 Page capture to PDF; document route detection
- 2026-07-01 JavaScript pages handed to the browser lane; queues on cover_status

## June 2026
- 2026-06-30 Cover text extraction (pypdf, cryptography), page count, language
- 2026-06-29 Source page links
- 2026-06-10 to 06-28 Monitor, harvest, report library and the first workflows
