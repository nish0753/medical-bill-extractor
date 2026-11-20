# Architecture Context

**Guiding principle:** the LLM reads; deterministic code checks. Gemini turns page images into rows. Code verifies every value against the page text and the bill's own arithmetic, assigns line roles, and computes confidence. We never trust a total or count that the model computed itself.

## Stack

| Layer | Technology | Role |
|---|---|---|
| Language / runtime | Python 3.10 | Whole service (matches the current Dockerfile) |
| Framework | FastAPI, Pydantic | Async job API, request validation, output schemas |
| Pipeline runner | Plain Python functions (LangGraph removed) | Runs the stages in order, including the repair loop |
| PDF / images | pdf2image (poppler), Pillow, pypdf | Render pages; read the PDF text layer |
| OCR | RapidOCR (PaddleOCR's models on ONNX Runtime, CPU only) | Free, local; words with positions. Chosen over full PaddleOCR (about 1 GB and slow on CPU) and Tesseract (weaker on messy scans and small digits) |
| LLM | Gemini 2.5 Flash, Flash-Lite, Pro (free tier) via langchain-google-genai | Page classification, line extraction, repair |
| Queue and job state | SQLite | Job queue, job status, document metadata. One worker processing one job at a time, since files are uploaded one by one. The queue stays because long bills run for minutes under Gemini rate limits |
| File storage | Local folders | Original files, page images, page text, stage outputs (cache), results |
| Evaluation | Python + rapidfuzz | Answer-key scorer, with fuzzy matching of item names |
| Review page | FastAPI + Jinja2 templates + HTMX | Server-rendered pages in the same app; no separate front-end build |
| Deployment | The developer's own machine (a learning project); Docker image available | Production hosting and auto-scaling are deferred |

## System Boundaries

- `app/`: the service. Today it has `main.py` (routes and response normalisation), `extractor.py` (the old pipeline) and `schemas.py` (response models). The module layout for the new pipeline is decided in M1.
- `tests/`: pytest suite. Offline by default; tests that call the network are marked `integration`.
- `docs/context/`: these context files, the source of truth for scope and design.
- `../TRAINING_SAMPLES/` and `../TRAINING_SAMPLES_extracted/`: input bills and the answer key, kept outside the repo. Read-only except through an approved migration or the review page.

### Pipeline stages

Each stage stores its output, so stages can be re-run individually without spending model quota again.

| # | Stage | Input → output | Tool |
|---|---|---|---|
| 1 | Ingest | file → page images (200–300 DPI), rotation fixed, page hashes | pdf2image, Pillow, RapidOCR (rotation: if OCR at 0° finds little confident text, retry at 90°/180°/270° and keep the best) |
| 2 | Page text and positions (OCR) | page → words with positions | PDF text layer, **if it passes a quality check** (sample 7 has an unreadable embedded OCR layer); otherwise RapidOCR. Pages are OCR'd in parallel across CPU cores |
| 3 | Page classification | image + text → `page_type`, new-bill marker, column headers, handwritten, totals-only | Gemini 2.5 Flash-Lite |
| 4 | Line extraction | **one page** + carried-over context (bill ID, current section, column map) → every printed row | Gemini 2.5 Flash, structured output |
| 5 | Grounding | rows + page words → `source` box, or `weakly_grounded` / `ungrounded_value` | code |
| 6 | Arithmetic | rows → row, section, parent-child and bill check results | code |
| 7 | Role resolution | rows + page classes + checks → `billable` / `summary` / `breakdown`; amount-column choice | code (counting rules R1–R3) |
| 8 | Repair | failed sections → targeted re-ask ("rows add up to X, section prints Y"), at most 2 attempts | Gemini Flash (Pro if the free quota allows) |
| 9 | Second reading | failed or low-confidence sections → agree / disagree | Gemini Flash on the OCR text only, no image |
| 10 | Confidence and status | all signals → line confidence, document status | code |
| 11 | Output | `core` and `full` views; flagged documents go to the review queue | code |

### Confidence rules

| Signal | Effect |
|---|---|
| Ungrounded value | line confidence ≤ 0.3 |
| Weakly grounded value | ≤ 0.7 |
| Row maths fails | ≤ 0.5 |
| Section reconciles | lines in that section ≥ 0.95 |
| Bill reconciles | all billable lines ≥ 0.95 |
| Second reading disagrees | disputed lines ≤ 0.5 |
| Handwritten page | capped at 0.7 |

Bills and documents also get a confidence: the weakest line, capped by how much of the bill's arithmetic could be checked (reconciled 1.0, only sections checked 0.8, nothing checkable 0.7, mismatch 0.4). Details in `lld.md` §8.9.

Thresholds are tuned against trusted precision and confidence calibration. Because stages 4 and 9 both use Gemini, their errors may be correlated. Grounding and arithmetic are the primary defence; agreement between readings is supporting evidence only.

### Rate limits and caching

- A queue-based per-model budget (requests per minute and per day) keeps within free-tier limits. Jobs wait instead of failing.
- Model results are cached by page hash plus prompt version.
- Quota errors back off and retry. Validation errors go to repair or get flagged; they are never retried with the same input.
- Each document has a maximum number of model calls. Going over marks the document `flagged` with its partial results.

## Storage Model

- **SQLite database**: jobs (queue position, status, timestamps, errors) and document metadata (pages, bills, status). Small rows only.
- **Local folders**, under a configurable data directory:
  - original files and rendered pages, keyed by document ID and page hash;
  - per-page text with word positions, model outputs and stage outputs, keyed by page hash plus prompt version (this doubles as the cache);
  - results as gzipped JSON per document, about 500 KB uncompressed for a 90-page bill.
- Storage is reached only through a thin storage module, so a later move to Postgres or cloud storage doesn't touch the pipeline.

### Scaling Path (deferred)

When load needs more than one worker, the setup changes as follows. The pipeline code doesn't change, because it only uses the queue and storage modules.

| Today | Later | Why |
|---|---|---|
| SQLite queue | **RabbitMQ** | Hands each job to one of many workers and redelivers it if a worker dies |
| SQLite job status | **Postgres** | RabbitMQ can't answer status queries; job status needs a real database |
| Local folders | **Shared object storage** (S3-compatible, e.g. MinIO) | Workers on different machines need the same files |
| Rate limiting inside one process | **A shared rate limiter** (Redis) | The Gemini free-tier limit is per API key, across all workers |

RabbitMQ settings for this workload:
- Acknowledge a message only after its job is finished. Redelivery is cheap because cached pages aren't re-sent to Gemini.
- Prefetch = 1, since bills can take minutes.
- A dead-letter queue for jobs that keep failing.
- Optionally one message per page for large bills, with the bill-level checks run once all of its pages are done.

## Observability (planned, after M4)

**Tool: LangSmith** (owner's decision, 2026-11-20). It is a hosted service, so there is nothing to run locally. It is used through the `langsmith` SDK (`@traceable` spans), which doesn't need LangChain.
- All tracing goes through one small interface in `core/` (a `span(name, **attributes)` context manager). Only that module imports the LangSmith SDK; tracing is off unless `LANGSMITH_API_KEY` and `LANGSMITH_TRACING=true` are set.
- Trace shape: one trace per job; a span per pipeline stage; a child span per model call with model, prompt version, tokens, latency, cost and cache hit; check results, confidence and status as span metadata. Evaluation runs are sent as LangSmith datasets/experiments, with the scorer's metrics as feedback scores.
- **Tracing fails open:** if LangSmith is unreachable or slow, jobs still run normally. Spans are sent in the background; when they can't be sent, they're written to a local JSONL file under the data folder instead of blocking or failing the job.
- **Privacy:** traces go to LangSmith's servers, a third party. By default, page images, OCR text and patient details are kept out of traces (masked inputs/outputs); item names and amounts are included only if the owner turns that on. Retention follows the LangSmith plan.
- **Limits:** the free LangSmith plan caps traces per month; check the current quota before enabling tracing on large runs, and sample evaluation runs if needed.

## Auth and Access Model

No users or access control are defined yet. The review page will need at least a shared secret before it is deployed anywhere public; this is tracked in `progress-tracker.md`.

## Security and Data Handling

- **Secrets**: the Gemini API key is read from the `GOOGLE_API_KEY` environment variable or an untracked `.env` file, and never committed.
- **Sensitive data**: bills contain patient names, addresses and diagnoses. Full pages are sent to the Gemini **free tier**, which may use inputs to improve Google's products. **The owner accepted this risk** (2026-11-19). Revisit before real production use.
- **Retention**: not decided yet (tracked in `progress-tracker.md`).

## Invariants

1. The LLM never computes totals, counts, roles or confidence. Code does.
2. A value is never altered to make a bill reconcile. Mismatches are reported.
3. Values that aren't printed are `null`, never `0`.
4. Every line has a role. A bill's `billable_sum` includes only `billable` lines.
5. `page_no` is the physical page in the file, assigned by code rather than by the model.
6. Request handlers never run extraction. They only enqueue jobs and read results.
7. No model call happens without a cache key (page hash plus prompt version).
8. A failed validation is never retried with the identical input.
9. All queue access goes through the queue module, and all file and database access through the storage module. Nothing else imports SQLite, the file system paths or (later) RabbitMQ directly.
