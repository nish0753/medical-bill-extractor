# Code Standards

## Commands

| Task | Command |
|---|---|
| Install | `python3.10 -m venv .venv && .venv/bin/pip install -r requirements.txt rapidfuzz pyyaml` (plus the system package poppler: `brew install poppler` on macOS). Use the project `.venv`, never a global install. RapidOCR is added to requirements in M1 |
| Run locally | `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload` |
| Test (all, offline) | `pytest -q -m "not integration"` |
| Test (one) | `pytest tests/test_unit.py::TestLangGraphBatchAggregation -q` |
| Test (with network) | `pytest -q` (needs `GOOGLE_API_KEY`) |
| Rebuild answer key v2 | `python -m eval.migrate_answer_key` (`--check` validates without writing) |
| Run the old pipeline on all samples | `python -m eval.run_baseline --out eval/outputs/<run>` (needs `GOOGLE_API_KEY`; resumable) |
| Evaluate against the answer key | `python -m eval.run_eval --outputs eval/outputs/<run> --format legacy\|contract --label <name> --compare` (exit code 2 = regression) |
| Lint / format | None configured yet |
| Build | `docker build .` |

## General

- Keep each pipeline stage a separate unit with explicit inputs and outputs, so it can be tested and re-run on its own.
- Fix root causes. Don't add retries or fallbacks to hide wrong output.
- Prompt text is versioned. Any prompt change bumps its version, which invalidates the cache and is evaluated before merging.

## Python

- Use type hints on all public functions. Model output is parsed into Pydantic models before anything uses it.
- Validate all external input (uploads, URLs, model responses) at the boundary.
- Read configuration through one settings object, not module-level `os.getenv` at import time. Today's import-time globals force tests to monkeypatch module attributes.
- Represent money as `Decimal`, or round consistently to 2 places, before comparing. Never compare floats for equality.

## FastAPI

- Route handlers only validate, enqueue and read results (architecture invariant 6).
- Failures return a consistent body with `status` and an `error` reason; no stack traces leak to the client.

## Interfaces (API / CLI / jobs)

- `POST /jobs` accepts a file or a URL (http/https only, with a size limit) and returns `job_id`.
- `GET /jobs/{id}` returns the status, then the result in `?view=core` or `?view=full`.
- The review endpoints list flagged documents and save corrections.

## Data and Storage

- Money values are stored exactly as printed (normalised numeric). `null` means not printed.
- Large artefacts (page images, results) go to file or blob storage, never into the job record.

## Testing

- Every unit ships with offline tests. Stub Gemini, OCR and PDF rendering, following the pattern in `tests/test_unit.py`.
- Tests that call external services use `@pytest.mark.integration`.
- To stub the extractor in API tests, patch `app.main.extract_bill_data`, not `app.extractor.extract_bill_data` (see `CLAUDE.md`).
- Changes to the pipeline or prompts also run the answer-key scorer once it exists.

## Review Page

- Server-rendered Jinja2 templates with HTMX for partial updates. No separate front-end build or JavaScript framework.

## File Organization

- `app/`: service code (API, pipeline stages, checks).
- `tests/`: pytest suite mirroring the `app/` modules.
- `docs/context/`: context files (this folder).
