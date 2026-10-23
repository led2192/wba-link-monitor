# Changelog

Newest first. One line per change, with the pull request. Every pull request adds its line here.

## October 2026
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
