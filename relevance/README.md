# Relevance workbook

The reviewers' spreadsheet (`WCD-documents-<N>-states.xlsx`) has one sheet per
state and one row per crawled document, plus a `Summary` sheet. Each row carries
an English name, a theme, a 1–5 relevancy score with its reason, and a checkbox
column for the manual review. This folder holds the scores and the script that
turns them into the workbook.

## Columns

| Column | Content |
|---|---|
| Name of Document | English title |
| Language of the document | Language of the source |
| Type of Document | One of the document types below |
| Date | `YYYY-MM-DD` where known |
| State | State name |
| Document Number | Order / notification number as printed |
| Document Type | **Theme**, one of the themes below |
| 2-3 line description of the document | Written from the extracted text, or `Pending OCR` plus the original title |
| Link to document | Original government URL |
| Relevancy Score | 1–5 |
| Whether document is relevant (Manual Check) | Reviewer's checkbox |
| Notes | `Score reason: …`, plus reliability caveats |

## Relevancy scale

| Score | Meaning |
|---|---|
| 5 | POCSO / child sexual abuse, child marriage, Juvenile Justice Act bodies and child care institutions, adoption, child trafficking, child labour |
| 4 | Child-protection systems and survivor support: ICPS / Mission Vatsalya, child helpline, One Stop Centres, domestic-violence protection officers, shelter homes, victim compensation, dowry prohibition, sexual-harassment committees |
| 3 | Broader child welfare with programme substance: child nutrition norms, pre-school education, girl-child and adolescent-girl schemes, creches |
| 2 | General department schemes and programme administration: women's welfare and livelihood schemes, anganwadi infrastructure and honoraria, general ICDS operations |
| 1 | Administrative, HR and finance: transfers, promotions, pay, seniority lists, recruitment rules, budget releases, procurement |

A staffing or recruitment order for a child-protection post is still an HR
document: score it 2 and say so in the reason.

## Allowed values

**Themes:** Child Sexual Abuse / POCSO · Child Marriage · Juvenile Justice / CCIs ·
Child Labour / Trafficking · Child Protection · Women's Safety / Violence ·
Nutrition / ICDS / Anganwadi · Education / ECCE · Health · Social Welfare Schemes ·
Administration / HR · Procurement / Finance · Other

**Document types:** Act · Rules · SOP / Guidelines · Scheme Guidelines ·
Government Order · Notification · Circular · Office Order · Letter · Notice ·
Report · Form · Recruitment / Transfer · Tender / Procurement · Other

`workbook.py` rejects any score record that uses a value outside these lists.

## Score records

Scores live in `scores/<state>.jsonl` (or `.json`), one record per document,
keyed by `pdf_url`:

```json
{"pdf_url": "https://…", "name": "Starting One Stop Centre - Sakhi scheme",
 "language": "Gujarati", "type": "Government Order",
 "theme": "Women's Safety / Violence", "score": 4,
 "reason": "One Stop Centre for women facing violence"}
```

Records written from extracted text also carry `description`, `date`,
`doc_number` and `caveat` (how far the text can be trusted: OCR quality, how
much of a long document was read, where the date came from).

The scores are a reader's judgement, one document at a time. A language model
did the reading for the files in `scores/`; a person can write the same records.
Do not replace the reading with keyword matching: titles such as "Order dated
26/02/2021" or a scheme name with no context need a judgement, and a wrong
score hides a document from the reviewers.

## Workflow

A state usually goes through two passes.

**1. Title-only pass**, as soon as the crawl metadata exists. Every document
gets a provisional score from its title, branch and listing type, so reviewers
can start filtering and OCR can be prioritised.

```bash
python relevance/workbook.py batches --metadata gujarat/crawl_metadata.json --out-dir /tmp/gj
# readers score each batch; concatenate their output into relevance/scores/gujarat.jsonl
python relevance/workbook.py title-only \
    --previous WCD-documents-6-states.xlsx --out WCD-documents-7-states.xlsx \
    --state Gujarat --metadata gujarat/crawl_metadata.json \
    --scores relevance/scores/gujarat.jsonl
```

The sheet is named `<State> (provisional)`, sorted by score, and every note
starts with `PROVISIONAL`.

**2. Text pass**, once the relevant documents have been OCR'd. Their rows are
rewritten from the extracted text and moved to the top of the sheet; rows
without a new record stay as they were, and reviewers' ticks are preserved.

```bash
python relevance/workbook.py summaries \
    --previous WCD-documents-7-states.xlsx --out WCD-documents-7-states.xlsx \
    --sheet "Karnataka (provisional)" --rename Karnataka \
    --scores relevance/scores/karnataka.json
```

Both commands recount the `Summary` sheet from the state sheets. Pass `--note`
to set the explanatory notes under the totals.

## Known limits of the current scores

- **Gujarat** is title-only for all 1,002 documents. The crawl covers the WCD
  department's resolutions listing, which holds no POCSO, juvenile-justice or
  child-marriage documents: one document scores 5 and 36 score 4, all on
  women's safety.
- **Gujarat consistency:** the batches were read independently, so near-identical
  titles can sit one point apart (for example ex-gratia orders for anganwadi
  workers at 1 and 2).
- **Karnataka:** two Gruha Lakshmi PDFs have a legacy-font text layer that
  extracts as gibberish. Their descriptions rest on the title until they are
  OCR'd from page images. Long documents were read from the start only.
