# Medical Bill Extractor

## Overview

A service that takes a hospital or pharmacy bill (PDF or image, 1 to 100+ pages, printed, scanned or handwritten) and returns **every line printed on it** as structured JSON. Each line has a role, so the bill total is counted exactly once, and carries evidence that its values really appear on the page. It's for anyone who needs reliable itemised bill data, such as insurance claim processing or expense audit. It replaces the original hackathon pipeline (Bajaj Finserv Datathon), which batched pages into a single Gemini call and never checked the results.

## Goals

1. Extract all printed line items: name, quantity, rate, amount, section, date, page.
2. Count each rupee once. Summary lines and breakdown lines are kept but flagged, never double-counted.
3. Never present an invented value as trusted. Every value is checked against the page text and the bill's own arithmetic.
4. Use zero paid model or OCR services (free-tier Gemini plus local OCR).
5. Meet the accuracy targets set after the M0 baseline (see Quality and Evaluation).

## Core Flow

1. A client submits one bill file at a time (file upload or URL) and receives a `job_id`. There is no bulk upload. One file can still contain several bills, which are split out in `bills[]`.
2. A worker processes the job through the extraction pipeline (see `architecture.md`).
3. The client polls the job and receives the result in the `core` or `full` view.
4. Results that fail checks or have low confidence are marked `flagged` and appear on the review page.
5. A reviewer corrects the flagged lines. The corrected document is added to the answer key.

## Features

### Extraction

- Handles digital, scanned, rotated, handwritten and partial (excerpt) bills, and damaged PDF streams.
- Splits files that contain several separate bills (even on one page) into separate `bills[]` entries.
- Returns every printed line, with numbers exactly as printed. A value that isn't printed is `null`, never `0`.

### Output contract

The structure:

```
document: document_id, source_file, page_count, status (passed | flagged | failed), confidence, confidence_reason
├─ pages[]: page_no, page_type, rotation_applied, text_source (pdf_text | ocr), is_handwritten
└─ bills[]: bill_id, label, page_nos[], is_partial, confidence
   ├─ printed: {subtotal, discount, tax, round_off, grand_total}   (null when not printed)
   ├─ extracted: {billable_sum}
   ├─ reconciliation: {status: passed | mismatch | not_checkable, difference, reason}
   ├─ sections[]: {name, printed_subtotal, extracted_sum, status}
   └─ lines[]
```

Line fields:

| Field | View | Notes |
|---|---|---|
| `line_id`, `page_no`, `section`, `date` | core | `page_no` is the physical page in the file. Numbering is global, never per batch |
| `item_name`, `item_quantity`, `item_rate`, `item_amount` | core | As printed. Wrapped names are joined. `null` when not printed |
| `role` | core | `billable` \| `summary` \| `breakdown` |
| `covered_by` | core | For summary and breakdown lines: the lines or section that carry the money instead |
| `flags` | core | e.g. `ungrounded_value`, `weakly_grounded`, `row_math_mismatch`, `handwritten_uncertain`, `negative_return` |
| `confidence` | full | 0–1, computed from the checks. Not the model's own estimate |
| `source` | full | `{page_no, bbox}` of the amount on the page image |
| `gross_amount`, `discount_amount` | full | When the bill prints Gross and Net columns separately |

**Document confidence** (core view, added 2026-11-20 at the owner's request) is a number from 0 to 1: how likely it is that the **whole** document is correct, meaning every line is read right and given the right role. Code computes it from the checks, never from the model's own estimate. `confidence_reason` names what limits it in plain words, e.g. "partial document: no printed total to check against" or "2 amounts not found on the page". Each bill carries its own `confidence` too. A `flagged` document always has confidence below 0.7. The numbers are calibrated: documents scored around 0.9 should be fully correct about 90% of the time on the answer key (see Quality and Evaluation).

The `core` view keeps the legacy field names (`item_name`, `item_amount`, `item_rate`, `item_quantity`, `page_no`, `page_type`). Size is about 250 bytes per line in the full view, so a 90-page bill is about 500 KB (about 50 KB gzipped). Check details are stored per section and bill, not per line.

### Counting rules (each rupee counted once)

Each rule comes from a real case in `../TRAINING_SAMPLES/`.

| # | Rule | Samples |
|---|---|---|
| R1 | Summary and detail both present: if the detail is complete (adds up to the summary figure), detail lines are `billable` and summary lines are `summary`. If the detail is incomplete, the reverse: summary lines `billable`, detail lines `breakdown` | 4, 10, 14 |
| R2 | A parent row with breakdown rows beneath it: the parent is `billable`, the children are `breakdown`. If the children don't add up to the parent, flag it | 13 |
| R3 | Amount column: use the column holding the **charged** amount, the one that agrees with the printed subtotals. E.g. Net, not Gross; "Company Amount" when "Amount" is all 0.00 | 12, 15 |
| R4 | Tax: record line amounts exactly as printed. Reconcile with the bill's own formula (tax-inclusive: lines + round-off = net; tax-exclusive: subtotal + tax − discount ± round-off = net) | 2, 5 |
| R5 | Returns and refunds stay negative and remain `billable` | 13 |
| R6 | Several bills in one file or on one page become separate `bills[]` entries, each reconciled on its own | 2, 5, 7, 11 |
| R7 | Partial documents: `is_partial: true`. Reconciliation is `not_checkable` unless a section can still be checked. Never invent a total | 9, 10, 12, 13, 15 |
| R8 | When the bill's own arithmetic is wrong, report the mismatch. Never adjust values to make it balance | 14 (₹262.69) |
| R9 | Handwritten items are extracted, with uncertain names flagged. Amounts are still checked against the written total | 3, 8, 11 |
| R10 | Printed subtotal and total lines are not line items. They go into `sections[].printed_subtotal` and `bills[].printed` | all |

### Hallucination policy

- Every amount, rate and quantity must match a token in that page's text (PDF text or OCR) after normalisation (commas, ₹, spacing).
  - A near match (one character off, in a known OCR confusion pair such as 1/7, 0/8 or 5/6) counts as `weakly_grounded`.
  - No match at all is `ungrounded_value`.
- Row check: rate × quantity = amount (± 0.01), applying any discount or Net rule the bill prints.
- Section and bill reconciliation as defined in R4.
- A document is `passed` only when every bill reconciles (or is legitimately `not_checkable`) and no value is ungrounded.

### Review page

- Lists documents with `status = flagged`, worst first.
- Shows the page image next to the extracted lines. Clicking a line highlights its `source` box.
- A reviewer can edit values, change roles, add missed lines and delete invented ones. Checks re-run on save.
- Saved corrections are added to the answer key.

## Scope

### In Scope

- An async extraction API (submit, poll, fetch `core` or `full`).
- The extraction pipeline, checks and confidence scoring.
- The evaluation scorer and answer-key maintenance.
- The review page.

### Out of Scope

- Patient demographics, diagnosis coding, insurance adjudication.
- Paid LLMs or paid OCR services.
- Redacting patient details before pages are sent to the LLM (declined; see `architecture.md`, Security and Data Handling).

## Success Criteria

1. A 90-page bill completes end to end as an async job within the free-tier limits.
2. Every output line carries a role, and no bill's billable sum counts a summary or breakdown line.
3. Every line in the `full` view has a `source` location, or is flagged `ungrounded_value`.
4. Flagged documents can be corrected on the review page, and the corrections reach the answer key.
5. The targets in Quality and Evaluation are met on the held-out set.

## Quality and Evaluation

- **Answer key**: `../TRAINING_SAMPLES_extracted/v2/` holds 15 hand-checked documents in the "all lines with roles" format (1,069 lines: 876 `billable`, 1 `summary`, 192 `breakdown`) for the bills in `../TRAINING_SAMPLES/`. Both folders sit beside the repo, not inside it. `eval/splits.yaml` holds 5 documents out as the test split; they are never used for prompt tuning. CORD (86 public receipts, `../EXTERNAL_DATASETS/cord/`) is an external check for unfamiliar layouts.
- **Metrics**, per bill and overall, with handwritten bills reported separately:
  - line recall and line precision (lines matched on page, exact amount and fuzzy name);
  - amount exact-match;
  - role accuracy;
  - bill total match;
  - ungrounded rate;
  - **trusted precision**: the share of `passed` documents that are fully correct. This is the key number.
- **Targets** (set 2026-11-20 from the M0 baseline; met when M6 is done). Pages are counted, never ignored. "Printed" excludes handwritten and partial bills, which are reported separately.

  | Metric | Baseline (old pipeline, Gemini) | Target, dev and test splits |
  |---|---|---|
  | Trusted precision (passed documents fully correct) | not measurable (no status) | **100% on today's splits; ≥ 97% once the held-out set has ≥ 100 bills** |
  | Flag recall (wrong documents that were flagged) | not measurable | ≥ 90% |
  | Line recall, printed bills | 63.9% (88.3% pages ignored), all 15 bills | ≥ 97% |
  | Line recall, handwritten bills | 61.5% (69.2% pages ignored) | ≥ 85% (flagged anyway) |
  | Line precision | dev 67.4% (96.7% pages ignored) | ≥ 98% |
  | Amount exact match (matched rows) | 97.1% | ≥ 99.5% |
  | Role accuracy | 64.1% (75.0% pages ignored) | ≥ 95% |
  | Bill total match, complete bills | 66.7% (printed) | ≥ 90%; every miss flagged |
  | Ungrounded rate, passed documents | not measured | ≤ 1%; 0 ungrounded lines in a passed document |
  | CORD recall (external, not a gate) | 84.4% (Qwen) | ≥ 90% |

  Why these numbers: the old pipeline already reaches 88–98% recall on printed bills when its page bug is set aside, so the redesign must beat that with pages counted. The weak areas it must fix are roles (double counting) and totals. Trusted precision is the key number, but 5 test bills can't prove 97%: the range around a 5-for-5 result is roughly 50–100%. That's why the held-out set grows with every bill approved on the review page, and the 97% target applies from 100 bills.
