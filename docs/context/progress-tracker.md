# Progress Tracker

Update this file after every meaningful implementation change.

## Current Phase

- M0 done (2026-11-20). Next: M1 (ingest, page text and positions, page classification).

## Current Goal

- Owner decision before the public demo and M2: the Gemini free-tier quota (see Open Questions).

## Completed

- `CLAUDE.md` for the existing code (2026-11-19).
- Answer key: all 15 training samples hand-transcribed and checked. Every row's rate × quantity was checked against its amount, plus section and bill totals and the parent/breakdown sums. Saved to `../TRAINING_SAMPLES_extracted/` (2026-11-19).
- Spec and architecture drafted (2026-11-19), then converted to these context files (2026-11-20). The old `docs/spec.md` and `docs/architecture.md` were deleted.
- Low-level design written: `lld.md` (2026-11-20).
- P1–P7 approved by the owner (2026-11-20, "time to build").
- M0, part 1 (2026-11-20):
  - Answer key v2: `../TRAINING_SAMPLES_extracted/v2/`, 1,069 lines with roles (876 `billable`, 1 `summary`, 192 `breakdown`). Sample 13's 174 breakdown rows were transcribed from the page images (`source/children13.py`). The transcriptions were moved out of the session scratchpad into `../TRAINING_SAMPLES_extracted/source/`.
  - Scorer: `eval/` (adapters, matcher, metrics, run_eval, run_baseline, splits.yaml) and `tests/eval/test_scorer.py`. The key scores 100% against itself; the v1 key scored as old-pipeline output misses exactly the 193 summary/breakdown lines v1 dropped.
  - Project virtualenv `.venv` (Python 3.10). `.gitignore` excludes `eval/outputs/` and `eval/reports/`; `.dockerignore` excludes `.venv/` and `eval/`.

## In Progress

- LangSmith skills installed (2026-11-20) from langchain-ai/langsmith-skills into `../.claude/skills/` (7 skills; the LangSmith CLI installer was not run). Tracing now follows the langsmith-trace skill: `@traceable` on the plain-Python steps, traces addressed to agent `medical-bill-extractor` / `local`. LangChain upgraded to 1.x (`langchain-core` 1.7.7, `langgraph` 1.2) because LangSmith rejected agent-addressed runs from the 0.3 tracer; offline suite and one real Gemini call pass on the new versions.
- LangSmith tracing on the current pipeline (2026-11-20): `app/tracing.py` (setup, `span()`, `scrub()`, flush), spans on both API endpoints, `extract_bill` and `gemini_call`, LangChain/LangGraph auto-traces masked by the same client; eval runs flush traces; `tests/test_tracing.py` checks what would actually be sent. Smoke trace reached the owner's LangSmith.
- M0 complete (2026-11-20): baseline recorded (`eval/baselines/latest.json`, old pipeline + Gemini 2.5 Flash) and targets written into `project-overview.md` (Quality and Evaluation). Work committed on branch `m0-eval`.
- Model comparison on the old pipeline (2026-11-20), same prompt, images, OCR and batches; scored with pages ignored because the old pipeline copies the model's per-batch page numbers (fixed by invariant 5):

  | dev split (pages ignored) | Gemini 2.5 Flash (free) | Qwen3.6-35B-A3B (Modal B200, SGLang) |
  |---|---|---|
  | Line recall | 93.3% | 90.4% (skips sample 13's breakdown rows) |
  | Line precision | 96.7% | 99.3% |
  | Amount exact match | 97.1% | 99.7% |
  | Role accuracy | 75.0% | 82.2% |
  | Docs fully correct | 5/10 | 6/10 |
  | Time for 15 bills | 16.8 min (+ quota waits) | 3.7 min |

  With page numbers counted (the real contract), Gemini dev recall is 65.0% and Qwen 45.7%. (Scores updated after the matcher fix below.) Qwen on CORD (86 receipts): recall 84.4%, precision 85.2%; most misreads are Indonesian thousands separators read as decimals ("25.000" -> 25.00).
- Matcher fix (2026-11-20): names are compared lowercased with punctuation turned into spaces, so "Equipment(999311)" matches "Equipment" (sample 10). Baseline re-saved; changes were +0.3–0.4 points.
- Page-number fix on the old pipeline (2026-11-21): `build_batches` records each batch's physical page numbers, and `validate_aggregate` maps the model's labels onto them through `_resolve_page_no`. It accepts relative (1..k) or absolute labels, forces single-page batches to the physical page, falls back to entry order when labels clash (e.g. all "1"), merges duplicate entries for the same page and sorts by page. This applies invariant 5 early. Tests in `TestLangGraphBatchAggregation` cover batch sizes 1, 2, 3 and 5. The baseline in `eval/baselines/latest.json` predates this fix, so the page-counted scores above should improve on a re-run.
- Trace images opt-in (2026-11-21, owner's request): `LANGSMITH_INCLUDE_IMAGES=true` keeps the page images sent to Gemini in LangSmith traces, as a shrunk copy (max side 1200 px, JPEG quality 55: a sample page goes from 836 KB to 95 KB; Gemini still gets the original); raw OCR text and file bytes stay masked. Default off; on in the local `.env`. Test: `test_include_images_keeps_image_but_still_drops_ocr_and_bytes`.
- Self-hosted comparison endpoint **deployed** (2026-11-21): `serving/modal_vllm.py` as Modal app `bill-vlm` on 1× A100-80GB, URL in `.env` as `MODAL_VLLM_BASE_URL`; vLLM 0.33.0 serves Qwen3.6-35B-A3B with `--enforce-eager`. Measured: weights pre-fetched to the `hf-cache` volume in 8 min (one-off); cold start about 3–4.5 min (weights 78 s, 65.5 GiB, then ~2.5 min of vLLM memory profiling, plus container start); warm `/v1/models` 1 s; KV cache 3.2 GiB = 136K tokens (4 concurrent 32K requests), so the A100 fits. Smoke test: sample 1 (2 pages, 1 call) took 150 s on the warm GPU and returned 38 items with physical page numbers (sample 1 is in the test split: aggregate check only). Fix found: Modal answers requests over ~150 s with a 303, so `eval/backends.py` now follows redirects. Next: the full 15-sample comparison with page numbers counted.
- Comparison run on the A100, attempt 1 stopped (2026-11-21): with `--enforce-eager` generation ran at 18 tokens/s (~150 s per 2-page call). Turning it off made vLLM crash at startup (default `max_num_seqs` 256 > 114 Mamba cache blocks for CUDA-graph capture) and Modal restarted it in a loop; the owner stopped the container. Fixed with `--max-num-seqs 16` and eager off, redeployed, not yet started. Only sample 1 (eager mode) is saved in `eval/outputs/qwen36-self-a100/`. Lesson: test a config change with one request before a full run.
- **Self-hosted Qwen comparison done (2026-11-21)**, H100, vLLM 0.33.0, page-number fix in place, run `eval/outputs/qwen36-self-h100`: dev line recall 90.7% **with page numbers counted** (same as with pages ignored, so the page fix works), precision 99.4%, amount exact 99.7%, role accuracy 82.1%, docs fully correct 6/10 dev, 8/15 overall. Matches the 2026-11-20 SGLang/B200 run (90.4% / 99.3% / 99.7% / 82.2%), so engine and GPU don't change quality. Speed: 15 bills in 11.2 min including a ~3.5 min cold start (warm: 2–93 s per bill, 130–160 tokens/s); cost about $1.10. Gemini has not been re-run with the page fix (free-tier quota), so its page-counted score (65.0%) is still the buggy one.
- Self-hosted Qwen on CORD (2026-11-21, H100, `eval/outputs/qwen36-self-h100-cord`): all 86 receipts ok, 2.8 min of model time (~2 s each), about $0.40. Recall 83.9%, precision 85.1%, amount exact 85.9%, 62/86 fully correct, vs 84.4% / 85.2% / 86.0% / 63 on 2026-11-20 (B200, SGLang): the same within one receipt. The known weakness stays: Indonesian thousands separators read as decimals.
- **OCR benchmark (2026-11-21, owner's request): PaddleOCR vs Tesseract.** `eval/ocr_bench.py` (render, score) + `serving/modal_paddleocr.py` (PaddleOCR 3.7.0, PP-OCRv5 server det/rec, doc-orientation on, PaddlePaddle-GPU 3.3.1, Modal L4, ephemeral). Metric: share of answer-key amounts whose number appears in the OCR text of the right page (50 pages, 200 DPI). PaddleOCR 97.1% dev / 98.4% test vs Tesseract (today's pipeline) 91.0% / 91.8%; rates 97.9% vs 91.5% dev. Both ~1.5 s/page (PaddleOCR on the L4, Tesseract on the M2 CPU). Biggest gains: sample 7 (0/4 -> 4/4), 14 (84.8% -> 94.4%), 12 (92% -> 100%). Both miss sample 3's two handwritten amounts. GPU cost a few cents; the one-off image build took ~13 min. Report `eval/reports/2026-11-21_ocr-bench.md`. Open follow-up for M1: RapidOCR (the chosen engine) with the same PP-OCRv5 server models on CPU, to see whether it reaches PaddleOCR's score for free.
- **RapidOCR with PP-OCRv5 server models on CPU (2026-11-21):** RapidOCR 3.10.0 + onnxruntime 1.23.2 (the last release for Python 3.10), `max_side_len` raised to 4000 so pages aren't shrunk. Amounts on the right page: **99.1% dev / 99.7% test**, the best of the three (PaddleOCR GPU 97.1% / 98.4%, Tesseract 91.0% / 91.8%); it even reads one of sample 3's two handwritten amounts. But far too slow on the M2: median 29 s/page, 35 min for 50 pages (one page 669 s). A 90-page bill would need ~45 min of OCR. Installed in `.venv` only, not in requirements.txt yet. Next: the CPU-sized models (PP-OCRv6 small/medium, RapidOCR's default family) to find the accuracy/speed trade-off; the server models remain an option on a GPU.
- Tracing on Render (2026-11-21): no traces arrived because Render never had the LangSmith variables (`.env` is not in the image; `render.yaml` only set `GOOGLE_API_KEY`), and the old "tracing on" log line was lost because `tracing.setup()` runs before logging is configured. Fixed: `tracing.status()` is logged by `app/main.py` at startup with the reason when off; `render.yaml` lists the LangSmith variables (key `sync: false`, environment `production`, images off).
- Runner notes: `eval/backends.py` swaps only the model call (OpenAI-compatible API); images are re-saved as RGB because SGLang's Qwen processor rejects grayscale.

## Next Up

Milestones:

| # | Milestone | Done when |
|---|---|---|
| M0 | Migrate the answer key to "all lines with roles", build the scorer, baseline the current code | Baseline metrics recorded. Targets written into `project-overview.md` |
| M1 | Ingest, page text and positions, page classification (stages 1–3) | Every sample page has words with positions, a correct `page_type` and correct bill boundaries |
| M2 | Extraction, grounding, arithmetic (stages 4–6) | Line recall and ungrounded rate measured. No regression from the baseline |
| M3 | Role resolution, repair, second reading (stages 7–9) | Samples 4, 10, 13 and 14 get the correct roles. Repair fixes failed sections |
| M4 | Async API, queue, storage, confidence | A 90-page bill completes within free-tier limits |
| M5 | Review page | Flagged documents can be corrected; corrections reach the answer key |
| M6 | Tune to targets | Targets met on the held-out set |

## Open Questions

- ~~GPU for the self-hosted comparison endpoint~~ Resolved 2026-11-21: the owner chose 1× A100-80GB (default `SERVE_GPU` in `serving/modal_vllm.py`).

- Production auto-scaling: deferred. The public demo goes on Hugging Face Spaces (decided 2026-11-20); revisit real production hosting after M6. The current `render.yaml` won't suit the SQLite + local files setup (Render's free plan has no persistent disk). The planned scaling path is in `architecture.md`, Scaling Path.
- Maximum file size and page count per job (today's limit is 20 MB).
- **Public demo quota.** Gemini is the live model and the demo will be shown to interviewers, but the free tier allows 20 requests per model per day (about 4 ten-page bills). Options: enable paid Gemini with a budget alert (recommended; roughly ₹1–3 per 10-page bill), or stay free and show a clear "daily demo limit" message.
- GPT-OSS-120B on Groq (owner asked, 2026-11-20): text-only, so it can't replace Gemini for reading page images. Free plan: 30 requests/min, 1,000/day, 8K tokens/min, 200K tokens/day (Groq docs table). Candidate for stage 9 (second reading works on OCR text only), where a different model family also removes the correlated-errors concern. Decide in M3.
- Can the Gemini free tier process a 90-page bill in acceptable time? **Measured 2026-11-20: no, as designed.** The free tier allows only 20 requests per day per model (`GenerateRequestsPerDayPerProjectPerModel-FreeTier`, gemini-2.5-flash and gemini-flash-latest). One page per call means one 90-page bill alone needs ~90+ calls. Needs an owner decision before M2: batch several pages per call, spread calls across the free models (each has its own daily quota), accept multi-day processing, or revisit the no-paid-model rule. The old pipeline's fallbacks gemini-2.0-flash and gemini-2.5-flash-lite return 404 for this key.
- Privacy: revisit the decision to send full pages to the free tier before any real production use.
- Result retention period.
- Access control for the review page (at minimum a shared secret) before any public deployment.
- Create `ui-context.md` for the review page before M5.
- LangSmith free plan's monthly trace quota: check before tracing large eval runs (a 15-bill run is ~100 runs including the auto-traced nodes and model calls).
- Module layout of `app/` for the new pipeline: decide in M1.
- RapidOCR, to be verified in M1 on the 50 sample pages:
  - seconds per page on the M2;
  - the share of answer-key amounts it finds on the right page;
  - whether the 90°/180°/270° retry correctly fixes sideways scans (sample 3).


## Architecture Decisions

- **Every printed line is returned, each with a role** (`billable` / `summary` / `breakdown`). Why: the owner wants all lines, not just totals, and the samples show summary/detail and parent/breakdown duplication that would otherwise double-count.
- **An extended schema with `core` and `full` views.** Why: the owner was concerned about data size. Measured at about 250 bytes per line, so the full view is cheap, and `core` stays lean and close to the legacy schema.
- **Free-tier Gemini only; free local OCR.** Why: the owner requires zero model cost. (Under review for the public demo: see the quota open question.)
- **Gemini is the live model** (2026-11-20). Why: the owner's choice for the deployed demo. Qwen on Modal stays an offline comparison backend only.
- **Comparison models are deployed from our own script (2026-11-21).** Why: the Modal-website endpoint chose GPU and engine itself; the owner wants control. `serving/modal_vllm.py` (vLLM, settings by env var, proxy-auth), client backend `modal-vllm`. See `architecture.md`, Self-hosted Comparison Endpoint.
- **Public demo on Hugging Face Spaces** (2026-11-20). Why: free, no card, a permanent link an interviewer can open; runs the existing Dockerfile.
- **LangSmith tracing now, rows but no page images** (2026-11-20). Why: the owner wants every trace in LangSmith; images and raw OCR text stay out to limit patient data in a third-party service.
- **Full pages are sent to the free tier, privacy risk accepted.** Why: the owner's choice. Redaction was offered and declined.
- **Async jobs, accuracy first.** Why: bills run to 90+ pages, and free-tier rate limits need queueing.
- **A simple review page for flagged results.** Why: a human closes the loop, and corrections grow the answer key.
- **Accuracy targets are set after a baseline.** Why: the owner prefers to measure the current pipeline before committing to numbers.
- **One page per model call, with carried-over context.** Why: today's 2-page batches cause wrong page numbers and hide cross-page context.
- **The stack is set (2026-11-20):**
  - plain Python stages (LangGraph dropped);
  - SQLite plus local folders for the queue and storage;
  - Jinja2 + HTMX for the review page;
  - rapidfuzz for the scorer;
  - runs on the owner's machine.

  Why: it's a learning project. The pipeline is a straight sequence that doesn't need a graph framework, and nothing here needs hosting or paid infrastructure yet.
- **RapidOCR is the OCR engine (2026-11-20).** Why:
  - The project runs on CPU only (Apple M2, no GPU).
  - RapidOCR gives PaddleOCR-level accuracy on scans and tables, with a much lighter install and decent CPU speed.
  - OCR accuracy matters more than speed, because OCR is the hallucination check. Gemini's free-tier rate limits, not OCR, set the overall processing time.
  - Tesseract and full PaddleOCR were dropped.
- **Document-level confidence in the output (2026-11-20).** Why: the owner wants one number saying how likely the whole result is correct. It is computed by code (weakest line, capped by what the bill's arithmetic could verify), never taken from the model, and must be calibrated on the answer key before it's trusted. Fields: `document.confidence`, `document.confidence_reason`, `bills[].confidence` (core view). Design in `lld.md` §8.9 and §11.3; built in M3.
- **CORD v2 receipts as an out-of-domain test set (2026-11-20).** Why: the owner wants results on data beyond the 15 bills. No public dataset of real Indian hospital bills was found. CORD (86 of 100 test receipts usable, 218 lines; converter `eval/datasets/cord.py`, data in `../EXTERNAL_DATASETS/cord/`) measures line reading only; it has no summary pages or parent/child rows, so it can't measure roles.
- **Bill checks add the bill's top-level lines (`billable` + `summary`), not just `billable` (2026-11-20).** Why: sample 14's main-bill total includes its PHARMACY CHARGES summary line. `billable_sum` still counts billable lines only, so nothing is counted twice across bills. Recorded in `lld.md` §8.5 with the implicit-rounding and settlement rules.
- **The answer key is rebuilt from the transcriptions, not from the v1 JSON (2026-11-20).** Why: v1 had flattened bills, sections and parent/child links away. The migration checks v2 against v1's totals, so nothing was lost.
- **LangSmith for observability, after M4 (2026-11-20).** Why: owner's choice; it's hosted, so no local Docker is needed. Isolated behind one tracing interface in `core/` (built in M1), off by default, failing open, with patient data masked.
- **Code does all maths, roles and confidence.** Why: the model's own totals can't be trusted, while arithmetic checks caught every error during the manual extraction.

## Session Notes

- Patterns in the samples that drive the design:
  - summary vs detail (4, 10, 14);
  - nested parent/breakdown rows (13);
  - wrong amount column (12: Gross/Net; 15: "Amount" all 0.00);
  - tax-inclusive line amounts (2, 5);
  - several bills per file (2, 5, 7, 11);
  - excerpts with no total (9, 10, 12, 13, 15);
  - the bill's own ₹262.69 inconsistency (14);
  - handwritten (3, 8, 11);
  - a garbage embedded text layer (7).
- Samples 9 and 10 are excerpts of the same 90-page bill. Sample 5's ₹71,925 implant invoice also appears as a line in sample 6.
- Sample 10's text layer contains a real patient's name, address and phone number. Keep these out of logs, docs and the answer key.
- Known bug in the current tests: `tests/test_api.py` patches `app.extractor.extract_bill_data`, which doesn't affect the endpoints. The tests pass only because the failure response has the same shape.
