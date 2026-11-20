# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Context docs (read first)

The project is being redesigned spec-first. Before implementing anything, read `docs/context/`:
- `project-overview.md` covers what to build: the output contract, counting rules and evaluation.
- `architecture.md` covers how: pipeline stages, models and invariants.
- `code-standards.md` covers commands and conventions.
- `lld.md` covers the low-level design: modules, data models, tables, API contracts, check algorithms and the evaluation harness.
- `progress-tracker.md` covers the current milestone, open questions and decisions.

Follow `docs/context/ai-workflow-rules.md`, and update `progress-tracker.md` after each completed unit. The sections below describe the **current** (pre-redesign) code.

## What this is

A FastAPI service that extracts itemized line items from bill/invoice images and PDFs. It converts each document into page images, adds optional Tesseract OCR text, and sends both to Gemini (via LangChain 1.x structured output) inside a LangGraph pipeline. The JSON response schema is fixed by the hackathon this was built for (Bajaj Finserv Datathon), so don't change field names or nesting in `app/schemas.py`.

## Commands

Use the project virtualenv (`.venv`, Python 3.10): prefix commands with `.venv/bin/` or activate it.

```bash
pip install -r requirements.txt rapidfuzz pyyaml   # also needs system packages: poppler-utils, tesseract-ocr
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload   # Swagger at /docs

pytest -q -m "not integration"           # offline suite (use this by default)
pytest -q                                # includes integration tests that hit Gemini (skip if no key or no quota)
pytest tests/test_unit.py::TestLangGraphBatchAggregation -q            # single test class or test

# Evaluation harness (eval/, M0). The answer key lives outside the repo in ../TRAINING_SAMPLES_extracted/v2/
python -m eval.migrate_answer_key --check                    # validate answer key v2 against v1 and the bills' own arithmetic
python -m eval.run_baseline --out eval/outputs/<run>          # run the current pipeline on all 15 samples (needs GOOGLE_API_KEY)
python -m eval.run_eval --outputs eval/outputs/<run> --format legacy --label <name> --compare
```

The `dev`/`test` split is in `eval/splits.yaml`. Only inspect individual failures on `dev`; the `test` split is for aggregate scores at milestone ends.

Docker: the image runs uvicorn on port 10000. `render.yaml` deploys it to Render and expects `GOOGLE_API_KEY` to be set there.

## Configuration

Set these as env vars or in a `.env` file in the project root. `load_dotenv(override=True)` means values in `.env` **override** variables already exported in the shell.

- `GOOGLE_API_KEY` (required, even when `USE_MOCK_MODE=true`: `extract_bill_data` checks it before doing anything else)
- `GEMINI_MODEL` (default `gemini-2.5-flash`), `GEMINI_MODEL_FALLBACKS` (comma-separated)
- `MAX_RETRIES`, `BATCH_SIZE` (pages per Gemini call), `PDF_DPI`, `USE_MOCK_MODE`
- LangSmith tracing: `LANGSMITH_TRACING=true` plus `LANGSMITH_API_KEY` turn it on; `LANGSMITH_ENDPOINT` and `LANGSMITH_AGENT_ID=medical-bill-extractor` / `LANGSMITH_AGENT_ENVIRONMENT` (`local`, `development`, `staging` or `production`) say where traces go. Don't also set `LANGSMITH_PROJECT`: with both, the SDK traces nothing; `APP_ENV` is added as trace metadata. All tracing goes through `app/tracing.py`, which masks page images, bytes and raw OCR text before anything is sent; see `architecture.md`, Observability. Tests force tracing off (`tests/conftest.py`).

All of these except `GOOGLE_API_KEY` are read **once at import time** into module globals in `app/extractor.py`. To change them in a test, use `monkeypatch.setattr(extractor, "BATCH_SIZE", ...)`. Setting the env var after import has no effect.

## Architecture

**`app/main.py`** is the API layer: `POST /extract-bill-data` (takes a JSON `{document: url}`), `POST /extract-from-file` (multipart upload), and `GET /health`. It downloads the file (20MB limit, http/https only), calls `extract_bill_data`, and then `build_response` normalizes the output. `build_response` casts numbers with `safe_float`, drops malformed items, and recomputes `total_item_count`. Extraction failures still return **HTTP 200** with `is_success: false` and an `error` string. Only request validation (422) and an oversized upload (413) return non-200 codes. `token_usage` is always zeros.

**`app/extractor.py`** holds the pipeline. `extract_bill_data(bytes, mime_type)` normalizes the MIME type. If the bytes start with `%PDF` it forces `application/pdf`, which handles `octet-stream` responses. It then runs a compiled LangGraph (a lazy singleton) with four nodes:
1. `prepare_pages`: PDFs go through `pdf2image` and come out as one JPEG per page. A single image goes through Pillow preprocessing (grayscale, contrast, sharpen). Each page gets optional OCR text. Any other MIME type raises.
2. `build_batches`: groups pages into chunks of `BATCH_SIZE` and joins their OCR text.
3. `extract_batches`: calls `call_gemini_api_async` once per batch, one batch at a time, with a 1s pause between calls.
4. `validate_aggregate`: merges `pagewise_line_items` from all batches.

`call_gemini_api_async` tries each model in `[GEMINI_MODEL, *fallbacks]`. On a 429 or quota error it retries with exponential backoff capped at 10s. If the error says the model has "limit: 0", it moves straight to the next model. LLM clients are cached per (key, model).

There are two parallel sets of Pydantic models: the `LLM*` models in `extractor.py` define Gemini's structured output, and `app/schemas.py` defines the API response (`FinalResponse`). Keep them in sync when changing fields.

OCR is optional. If the `tesseract` binary isn't found at import, the module-level `pytesseract` is set to `None` and OCR is skipped silently.

## Testing notes

- `main.py` does `from .extractor import extract_bill_data`, so `@patch('app.extractor.extract_bill_data')` does **not** replace the function the endpoints call. Patch `app.main.extract_bill_data` (and `app.main.download_file_sync`) instead.
- To test the pipeline offline, monkeypatch `extractor.convert_from_bytes`, `extractor.call_gemini_api_async` and `extractor.pytesseract = None`, and set a dummy `GOOGLE_API_KEY`. `tests/test_unit.py::TestLangGraphBatchAggregation` shows the pattern.
- Mark tests that call external services with `@pytest.mark.integration`. The marker is registered in `pytest.ini`.
