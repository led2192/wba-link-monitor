# Diagrams

The whole system in pictures. Each diagram also appears in the owner's guide ([guide.md](guide.md)) and on the Notion page, next to the text it illustrates. Last updated 24 October 2026.

## 1. The map: where things live and who writes them

```mermaid
flowchart LR
  subgraph Web["Company websites (about 2,000 companies)"]
    P["Report pages<br/>hubs, sustainability, IR, policies"]
    F["Report files<br/>PDFs"]
  end
  subgraph Cloud["GitHub Actions (cloud, every night)"]
    R["The robot<br/>monitor, harvest, read, type, date, score"]
  end
  subgraph PC["Owner's computer (every 4 hours)"]
    O["Office-network runner<br/>refused files and pages"]
  end
  subgraph AT["Airtable"]
    DL[("Disclosure Library<br/>companies, pages, documents, health")]
    QA[("Pipeline QA<br/>runs, failures, labels, evals")]
    SL[("Source Library 2024 / 2025<br/>analysts, read-only")]
  end
  subgraph C["Claude tasks"]
    GR["Daily gap research"]
    DLB["Daily labelling (Opus)"]
    WR["Weekly review (Opus)"]
  end
  TS[("Text store<br/>private repository")]
  P --> R
  F --> R
  P --> O
  F --> O
  R --> DL
  O --> DL
  R --> TS
  R --> QA
  SL -. read only .-> R
  GR --> DL
  DLB --> QA
  WR --> QA
  WR -- pull requests --> R
  Edi(("Owner")) -- merges, checks cards --> R
  Edi -- 5 cards a day --> QA
```

## 2. The life of one document

```mermaid
flowchart TD
  A["A report page is watched<br/>(monitored_links, type reports_hub...)"] --> B["Nightly monitor reads it<br/>new PDF link in new_links"]
  B --> C["Harvest: a report_library row<br/>(never twice, never a deleted one)"]
  C --> D["First type guess from the address<br/>source_type"]
  D --> E{"File attached?"}
  E -- "yes" --> G["First six pages read<br/>cover_text, language, pages"]
  E -- "site refuses" --> E2["file_status blocked:<br/>the robot on the owner's computer tries it"]
  E -- "over 5 MB" --> E3["read from the link<br/>(file_status too_big)"]
  E2 --> G
  E3 --> G
  G --> S{"Scanned PDF,<br/>no text?"}
  S -- "yes" --> S2["Text recognition (OCR), monthly"] --> H
  S -- "no" --> H["AI type and title (English)<br/>cover-title phrases (other languages)"]
  H --> I["Year decided (7 rules)<br/>scope, sustainability class"]
  I --> J["Full text at night<br/>fiscal year with score, standards, assurance"]
  J --> K["Company scored<br/>disclosure and page completeness"]
  K --> L["Pipeline QA records anything that went wrong"]
```

## 3. The night, hour by hour (Madrid time, winter)

```mermaid
flowchart LR
  T1["01:03 Browser monitor<br/>JavaScript pages"] --> T2["03:23 Monitor links"]
  T2 --> T3["Harvest"] --> T4["Classify by address"] --> T5["Attach PDFs"] --> T6["Build cover text"]
  T6 --> T7["Dedupe, titles, dates,<br/>non-English types"]
  T6 --> T8["Document scope"]
  T6 --> T9["Company coverage"]
  T9 --> T10["Base health"]
  T9 --> T11["QA sync"]
  T12["05:31 QA sample<br/>(weekdays)"]
  T13["05:40 Gone pages replaced,<br/>missing pages searched"]
  T14["22:37 Full text"] --> T15["Fiscal years,<br/>sustainability class,<br/>mention search"]
  T16["Every 4 hours: robot on the owner's computer"]
  T17["Every 30 minutes: Scan now, add_link, research_link"]
  T18["A failed job re-runs itself once"]
  T19["3rd of the month 07:13: text recognition of scanned PDFs"]
```

## 4. Which type is it? (the question order)

```mermaid
flowchart TD
  Q1{"This company's own disclosure?"} -- no --> X["Not a disclosure / wrong company: discard"]
  Q1 -- yes --> Q2{"A whole financial year with audited accounts<br/>and a directors' report?"}
  Q2 -- yes --> Q2a{"Which form?"}
  Q2a -- "US 10-K, 20-F, Philippine 17-A" --> T10K["10K Form"]
  Q2a -- "French, Spanish, Belgian URD" --> TURD["Registration Document"]
  Q2a -- "calls itself integrated" --> TIR["Integrated Report"]
  Q2a -- "accounts only" --> TFS["Financial Statement"]
  Q2a -- "otherwise" --> TAR["Annual Report"]
  Q2 -- no --> Q3{"Part of a year?<br/>quarter, half year, 10-Q"}
  Q3 -- yes --> TINT["Interim Reports"]
  Q3 -- no --> Q4{"Mainly sustainability,<br/>whole company, a period?"}
  Q4 -- yes --> TSR["Sustainability Report<br/>(or ESG, CSR, Impact by its own name)"]
  Q4 -- no --> Q5{"One topic or framework?"}
  Q5 -- yes --> TTOP["Climate, Environmental, Social,<br/>CDP, GRI, SASB, Engagement, Certifications"]
  Q5 -- no --> Q6{"A standing document?"}
  Q6 -- "code" --> TCOC["Code of Conduct"]
  Q6 -- "human rights" --> THRP["Human Rights Policy"]
  Q6 -- "other policy" --> TPOL["Policy Documents"]
  Q6 -- "AGM, articles, proxy, 8-K, 17-C, bond prospectus" --> TSTAT["Statutory Documents"]
  Q6 -- "slides" --> TPRES["Company Presentations"]
  HR["House rules: modern slavery statement = Social Reports;<br/>human rights statement = Human Rights Policy"]
```

## 5. Which year is it? (first rule that applies wins)

```mermaid
flowchart TD
  Y1{"A year in your curated title?"} -- yes --> R1["1 your title"]
  Y1 -- no --> Y2{"A regulator filing?"} -- yes --> R2["2 the filing's own year"]
  Y2 -- no --> Y3{"Full-text year, score 80+?"} -- yes --> R3["3 fiscal year (99% right)"]
  Y3 -- no --> Y4{"Imported from the Source Library?"} -- yes --> R4["4 the analyst's year"]
  Y4 -- no --> Y5{"Full-text year, score 60-79?"} -- yes --> R5["5 fiscal year (96% right)"]
  Y5 -- no --> Y6{"AI year whose quote was found in the document?"} -- yes --> R6["6 the AI's year"]
  Y6 -- no --> R7["7 a year in the title, file name or address"]
  N["The year is the year the reporting period ENDS:<br/>April 2024 to March 2025 = 2025"]
```

## 6. Completeness: six checks, two scores

```mermaid
flowchart LR
  subgraph D["Disclosure completeness (what WBA's work needs)"]
    D1["Sustainability report, last two years<br/>or a substantial embedded section"]
    D2["Annual report, last two years<br/>or a universal registration document"]
    D3["Code of Conduct"]
    D4["Human Rights Policy"]
  end
  subgraph G["Page completeness (health of our collection)"]
    G1["Report hub page watched and live"]
    G2["Sustainability page watched and live"]
  end
  D --> C["completeness = the six together<br/>(history kept comparable)"]
  G --> C
```

## 7. The quality loop

```mermaid
flowchart LR
  A["Scripts and qa_sync<br/>name every failure"] --> B[("Pipeline QA failures<br/>one row per distinct problem")]
  C["Daily cards, corrections,<br/>Source Library 2025"] --> L[("Labels<br/>the truth set")]
  B --> W["Weekly review (Opus)<br/>finds the costliest causes"]
  L --> W
  W --> PR["Pull request with a test<br/>and a measurement on real labels"]
  PR --> M{"Fixes more than it breaks?"}
  M -- yes --> MG["Owner merges"] --> RUN["Runs that night"] --> A
  M -- no --> W
```

## 8. The daily cards: Claude labels, the owner checks about 5

```mermaid
sequenceDiagram
  participant S as Sampler (05:31)
  participant C as Claude Opus (12:46)
  participant E as Owner (after 13:00)
  participant QA as Pipeline QA labels
  S->>QA: about 15 cards (disagreements, non-English, blind controls, new)
  C->>QA: type, year, title, confidence, quoted evidence on every card
  C->>QA: unticks the confident ones (they become Claude's labels)
  C->>E: about 5 left: low or medium confidence, high stakes, 2 random audits
  E->>QA: one click each: Agree / Disagree (correct one value) / Can't tell
  S->>QA: next morning: settles every card (labelled_by Edi or Claude)
  Note over QA: unanswered for 3 days = Claude's answer is kept
  Note over QA: Claude's labels never grade the Airtable AI
```

## 9. Files and pages a site refuses

```mermaid
flowchart TD
  A["Cloud robot is refused<br/>(file_status blocked, page status blocked)"] --> B["Robot on the owner's computer<br/>every 4 hours, ordinary connection"]
  B --> C{"Plain request works?"}
  C -- yes --> OK
  C -- no --> R{"robots.txt allows it?"}
  R -- no --> NO["Still refused, noted"]
  R -- yes --> D["Microsoft Edge without a window<br/>(no clicks, no captchas, no logins)"]
  D --> E{"Reached?"}
  E -- yes --> OK{"File size"}
  E -- no --> NO
  OK -- "5 MB or less" --> AT["Attached to the row"]
  OK -- "bigger" --> RD["First pages read on the computer<br/>(typed and dated, not attached)"]
  OK -- "a page" --> PG["Document links handed to the nightly harvest"]
  NO --> MON["Monday list: the few reports only a person can download"]
```

## 10. Who acts on each kind of failure

```mermaid
flowchart LR
  F1["Page does not open"] --> A1["Robot: retries nightly, browser lane"]
  F2["Page gone (404)"] --> A2["Robot: searches the site nightly, adds a clear match"]
  F3["Site refuses robots"] --> A3["Robot on the owner's computer"]
  F4["Scanned PDF"] --> A4["Robot: text recognition monthly"]
  F5["Non-English, no type"] --> A5["Robot: cover-title phrases"]
  F6["Type rules disagree"] --> A6["Weekly review: rule fixes, measured"]
  F7["Company missing a report"] --> A7["Daily research (Claude)"]
  F8["Job failed"] --> A8["Robot: re-runs once"]
  F9["Analyst flag"] --> A9["Weekly review checks it"]
  A1 & A2 & A3 & A4 & A5 & A6 & A7 & A8 & A9 --> E["Owner: at most 5 items a week<br/>(Monday list)"]
```

## 11. The daily research and how it is measured

```mermaid
flowchart LR
  Q["My queue: 25 companies<br/>with the most important gaps"] --> H["What the library already holds<br/>(skip it)"]
  H --> S["Web search, official sources only,<br/>right company only"]
  S --> RL["research_link<br/>+ research_gaps_before"]
  RL --> IN["Robot files and scans<br/>(marked found by research)"]
  IN --> M["After a week: gaps before vs now,<br/>share already held"]
  M --> D{"Worth it?"}
  D -- yes --> X["Scale to 50 a day"]
```
