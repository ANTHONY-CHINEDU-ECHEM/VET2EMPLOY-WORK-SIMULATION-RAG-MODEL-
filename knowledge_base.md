# Knowledge base

The knowledge base is everything the AI Manager is allowed to know. This document explains what it contains, how material is interpreted and how to change it safely.

## 1. Contents

| File | Source type | What it contributes |
| :--- | :--- | :--- |
| `Vet2Employ_Work_Brief.pdf` | `programme_brief` | Programme rules (Part A), four track briefs (Part B) and templates (Part C) |
| `Vet2Mploy_Brand_Strategy_Portfolio.pdf` | `brand_guideline` | Voice principles, tone by context, language rules, AI Manager guidance, style sheet |
| `Vet2Mploy_Plain_English_Report.docx` | `product_overview` | The candidate journey, the simulated company, the Verified Profile |
| `VEEP_Knowledge_Graph_Design_GCP.pdf` | `engineering_reference` | Architecture reference; excluded from task generation |
| 23 research workbooks (`.xlsx`) | `job_market`, `employer_review`, `methodology` | 521 UK roles with responsibilities, skills, tools, teams, culture and ratings |

Coverage of the research by role family and sector:

| Role family | Sectors covered |
| :--- | :--- |
| Cybersecurity | Construction, Consulting, Education, IT, general market |
| Data Analyst | Construction, Consulting, Education, Healthcare, IT, general market |
| Data Engineer | Consulting, Education, Healthcare, IT, general market |
| Software Developer | Construction, Consulting, Education, IT, general market |

## 2. Facets

Every chunk has one facet. Retrieval steps select by facet, so the facet decides when a passage can be used.

| Facet | Meaning |
| :--- | :--- |
| `programme_rules` | Programme wide rules outside any track (weekly rhythm, conduct, submission, assessment) |
| `supervisor_voice` | A track supervisor's welcome note |
| `track_client` | The fictitious client, stakeholders, problem, scope and constraints |
| `track_materials` | The working materials and their known issues |
| `track_schedule` | The week at a glance table |
| `track_activity` | Sequential activities; tagged with `activity_number` |
| `track_deliverables` | Deliverable references, file names, due points and acceptance criteria |
| `track_rubric` | Assessment rubric and weights |
| `track_stretch` | Stretch goals |
| `templates` | Appendices: timesheet, daily update, reflection log, peer review, glossary, checklist |
| `brand_voice`, `brand_general` | Brand guidance that governs language, and the rest of the brand portfolio |
| `product` | The Plain English Report |
| `engineering` | Engineering reference |
| `responsibilities`, `skills_experience`, `tech_stack`, `team_collaboration`, `work_culture` | Job role facets |
| `culture_ratings`, `employee_review`, `employee_perspective` | Employee sentiment evidence |
| `methodology` | Rating methodology and research data dictionaries |

## 3. Adding or updating material

### A new or revised workbook

1. Name it like the existing files so role family, sector and date are inferred, for example `Data_Engineer_Construction_Nov_12th_2026.xlsx`. Words that are recognised: role families (`Cybersecurity`, `Data_Analyst` or `Data_Analytics`, `Data_Engineer` or `Data_Engineering`, `Software_Developer`), sectors (`Construction`, `Consulting`, `Education` or `Educational`, `Healthcare`, a leading `IT_`), and a date in any of the existing styles.
2. Keep a sheet whose name contains `Master`. Other sheets are merged automatically when they carry `Company ID` and `Job Title`.
3. Drop the file into `knowledge_base/raw/` and run `v2e ingest` (or upload through `POST /v1/admin/documents`).
4. Read `var/reports/ingestion_report.md` for data quality findings.

If a name cannot follow the pattern, add an override to `config/knowledge_manifest.yaml`:

```yaml
workbooks:
  match: "*.xlsx"
  overrides:
    - match: "Quantum_Roles_2026.xlsx"
      role_families: [data_engineer]
      sectors: [information_technology]
      collected_on: 2026-11-02
```

### A new document

Add a rule under `documents` in the manifest with a glob, a title, a source type and a layout (`report` for numbered documents, `deck` for slide style documents), then ingest. Facet rules for the source type decide how sections are classified.

### A new track

Add the track to `config/tracks.yaml` (code, name, role family, file code and the regular expressions that identify its top level headings in the brief). Activities, deliverables, rubric and client sections are then tagged automatically when the brief is ingested.

### Removing material

`DELETE /v1/admin/documents/{document_id}` removes a document, its chunks and its role records in one transaction. Document ids are derived from file names, for example `doc_data_analyst_consulting_21st_oct_2026`.

## 4. Normalisation rules

| Issue in the source | Rule applied |
| :--- | :--- |
| `Company` versus `Company Name`, `Source URL` versus `Source URLs`, `Team Collaboration` versus `Team Collaboration & Culture`, trailing spaces in names | Canonical field names through an alias table; sheet names matched without case or spacing |
| `Mid-level`, `Mid-Level`, `Beginner ` with a trailing space | Four levels: beginner, mid level, advanced, expert |
| Company IDs repeated across workbooks | Every record id includes its source document; employers are linked by a normalised employer key |
| Mistyped Company ID on a topic sheet | Reattached by employer name and job title |
| `N/A`, blank or non numeric ratings | Dropped, never estimated |
| Ratings rows that duplicate a Master record | Merged into the record |
| Ratings rows for employers without a Master record | Kept as company culture records |
| Methodology and dictionary sheets repeated in every workbook | Stored once per distinct content |

## 5. Data quality findings on the supplied files

The ingestion of the supplied material completed with no failures. The pipeline resolved the following automatically and reports the one item it could not resolve:

* Column and sheet naming varied across all four batches and was unified.
* Several topic sheet rows carried Company IDs that differed from the Master (for example `NUF-0022` against `NUF-002` and `BRI-001` against `BHK-001`). They were reattached by employer and title.
* The Healthcare Data Analytics workbook's Tech Stack sheet has no Source URL column and its Work Culture sheet has no Niche column; values were taken from the Master.
* The Software Developer Construction workbook has more culture rows than roles; the extra rows are kept as company culture evidence.
* **Needs review:** in `Healthcare_Data_Analytics_2026-09-29.xlsx`, the Day to day job tasks row for `GSK-001` names a different job title from the Master record with the same ID. It is kept as its own record. The research team should confirm which title is correct.

## 6. Confidentiality

The Work Brief is classified Programme Confidential and the brand and design documents are Confidential. The `.gitignore` excludes `knowledge_base/raw/` from version control. Store the files in an approved location and mount them into the container read only, as `compose.yaml` does.
