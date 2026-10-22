# Documentation

The pipeline that keeps WBA's Disclosure Library current: company pages watched every night, reports harvested and read, gaps measured per company, and a quality log that records every way it fails.

| Document | What it answers |
| --- | --- |
| [architecture.md](architecture.md) | How the pieces fit: the nightly chain, the bases and tables, the on-demand paths |
| [scripts.md](scripts.md) | Every script and workflow: what it does, when it runs, what it writes |
| [quality.md](quality.md) | Pipeline QA: failure taxonomy, the QA base, golden set, daily and weekly review, the ship rule |
| [runbook.md](runbook.md) | How to do common things, and what to do when something breaks |
| [decisions.md](decisions.md) | Choices made, why, and what was rejected |
| [pitfalls.md](pitfalls.md) | Lessons that were costly to learn |
| [CHANGELOG.md](CHANGELOG.md) | What changed and when, newest first |
| [../TEXT_SEARCH.md](../TEXT_SEARCH.md) | The full-text store and mention search |

Field-level documentation lives in Airtable itself: every field in the Disclosure Library and the Pipeline QA base carries a description, and those descriptions are kept current with the code.

Instructions for Claude sessions working in this repository: [../CLAUDE.md](../CLAUDE.md).
