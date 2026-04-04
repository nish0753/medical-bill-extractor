# Video Recording Script — 8–10 Minute Walkthrough

A word-for-word talking guide structured around the five required sections. Read this once, then record naturally in your own words.

**Target length:** 8–10 minutes  
**Recording setup:** Screen share + camera (face in corner)

---

## Pre-Recording Checklist

- [ ] Service running locally: `python -m uvicorn app.main:app --reload`
- [ ] Browser tabs ready: `http://127.0.0.1:8000/docs` (Swagger UI)
- [ ] VS Code open with `app/extractor.py` and `app/main.py` side-by-side
- [ ] Sample medical bill PDF downloaded and ready to upload
- [ ] `.env` file has a valid `GOOGLE_API_KEY`
- [ ] Recording tool set to Screen + Camera

---

## Time Budget

| Section | What you cover | Duration |
|---------|---------------|----------|
| 1. Open the project | Intro + repo tour | ~1.5 min |
| 2. Walk through the code | `main.py` → `extractor.py` → `schemas.py` | ~3 min |
| 3. Design & trade-offs | Architecture decisions | ~2 min |
| 4. How you used AI | Runtime AI + dev-time AI | ~1.5 min |
| 5. Quick demo | Swagger UI live call | ~1 min |
| **Total** | | **~9 min** |

---

## Section 1 — Open the Project (~1.5 min)

> *Start on camera for 20 seconds, then switch to VS Code file tree*

**Say:**

> "Hey, I want to walk you through a project I built for the Bajaj Finserv Datathon — it placed in the top 50 out of several hundred submissions. It's called the **Medical Bill Extractor**. The problem it solves: hospitals give you a PDF or a photo of your bill, and the format is different every time. This API takes any of those documents, runs it through an AI pipeline, and returns a clean JSON with every line item — the service name, rate, quantity, and total charge — perfectly structured."

*Switch to VS Code file tree*

> "The repo is small and focused. The entire application lives in three files inside the `app` folder."

*Click open each file as you name it:*

> "`main.py` — this is the FastAPI layer. It handles routing, validates inputs, and normalizes the response before it goes back to the caller.
>
> `extractor.py` — this is the brain. Every step of the AI pipeline lives here: PDF conversion, image preprocessing, OCR, and the Gemini API calls.
>
> `schemas.py` — these are the Pydantic models that define exactly what the response looks like. Nothing in the public response is typed as 'any' — every field is explicit.
>
> Everything else is tests, a Dockerfile, and a `render.yaml` for one-click cloud deployment."

---

## Section 2 — Walk Through the Code (~3 min)

### 2a. `main.py` — The FastAPI Layer

*Screen: Open `app/main.py`, scroll to `BillRequest` validator (line 55)*

> "Let me start at the entry point. When a request comes in to `/extract-bill-data`, the first thing that happens is URL validation. Pydantic checks the scheme — it has to be `http` or `https`. Anything else, like `ftp://` or a bare string, is rejected with a 422 before it ever reaches the AI pipeline. This is a security boundary."

*Scroll down to `download_file_sync` (line 122)*

> "Once the URL is validated, we download the file. Notice I'm running this synchronous `requests.get` call inside `run_in_executor` — that keeps the async FastAPI event loop from blocking while we wait for the network. We also enforce a 20 MB file size limit here."

*Scroll to `safe_float` (line 75) and `build_response` (line 107)*

> "After the AI pipeline runs, `build_response` normalizes every field before returning it. `safe_float` handles whatever quirky format the model returns — a number, a string like '1,234.56', or even null — and converts it to a clean float without crashing. This is defensive programming for the API response layer."

---

### 2b. `extractor.py` — The AI Pipeline

*Screen: Open `app/extractor.py`, scroll to `_get_workflow` (line 237)*

> "Now for the brain. The pipeline is structured as a **LangGraph state machine** — four nodes wired together in sequence."

*Point to the four `add_node` calls:*

> "The four nodes are: `prepare_pages`, `build_batches`, `extract_batches`, and `validate_aggregate`. Data flows through them in order. Each node reads from a shared typed state and writes back only the keys it produces."

---

*Scroll up to `_prepare_pages_node` (line 128)*

> "The first node handles document preparation. For PDFs, `pdf2image` converts every page to a high-resolution image at 200 DPI. Then each image goes through `preprocess_image` — grayscale conversion, 1.5× contrast boost, 2× sharpness boost. This makes scanned text crisp before it hits the model."

*Scroll to `preprocess_image` (line 280)*

> "Here's that function. Three Pillow transforms in sequence. I arrived at these values through trial and error on the datathon's test bills."

---

*Scroll to `_build_batches_node` (line 188)*

> "The second node chunks pages into batches of two. This controls how many page images go into a single Gemini API call. Two pages keeps us safely under the token limit while cutting the number of API calls in half versus sending one page at a time."

---

*Scroll to `call_gemini_api_async` (line 341), stop at the prompt (line 352)*

> "The third node calls Gemini. Here's the extraction prompt. A few instructions here were not in the first version — they came from observing failures:
>
> 'Do NOT confuse IDs or Dates with monetary amounts' — early versions would see a date like 04/02/2026 and extract it as a charge.
>
> 'Do NOT extract Sub-total, Total, Tax, or Discount lines' — without this, the model would sometimes double-count the total.
>
> Each clause in this prompt represents a real failure mode I fixed."

---

*Scroll to `_validate_and_aggregate_node` (line 216)*

> "The last node merges the results from every batch into a single list. Crucially, `total_item_count` is **recomputed here from scratch** by counting actual items — we throw away the model's self-reported count because it was occasionally wrong."

---

### 2c. `schemas.py` — The Response Contract

*Screen: Open `app/schemas.py`*

> "Finally, schemas. `FinalResponse` wraps everything. `DataBlock` holds the pages. `PageLineItems` holds items per page. `LineItem` is the atomic unit — name, amount, rate, quantity. This strict schema is what the datathon scoring system consumed. Having it explicit in Pydantic meant the output was always the right shape, regardless of what the model returned."

---

## Section 3 — High-Level Design & Trade-offs (~2 min)

*Screen: Open `docs/wiki/System-Architecture.md` — show the Component Map diagram*

> "Let me explain three design decisions that mattered most."

**Trade-off 1: LangGraph vs. plain async functions**

> "I could have written this as a chain of async function calls. I chose LangGraph instead. The reason: during debugging, I needed to know exactly _which step_ produced a bad result. With LangGraph, every node is named and its output is logged independently. That transparency was worth the added structure."

**Trade-off 2: OCR + Vision vs. Vision alone**

> "The datathon dataset included some very low-quality scanned bills — phone photos of paper documents, slightly blurry, poor lighting. Vision alone on those would sometimes misread a digit. By running Tesseract OCR in parallel and appending its raw text to the prompt, I gave the model a second signal to cross-check against. When OCR and vision agree, the extraction is reliable. When they disagree, the model usually picks the clearer one."

**Trade-off 3: Model fallback chain**

> "The datathon used free-tier Google API keys. A single model would hit its quota mid-run. Instead of failing, I have three candidate models: `gemini-2.5-flash` as primary, and two fallbacks. The retry loop automatically moves to the next model when it gets a quota-zero response. This is what let the pipeline run continuously through the entire test set."

---

## Section 4 — How I Used AI (~1.5 min)

*Screen: Stay on VS Code, `app/extractor.py`, line 94*

> "AI was part of this project in two ways: as the runtime engine, and as a development tool."

**At runtime:**

> "The core technique is `with_structured_output` on the LangChain LLM — line 94 here. This tells Gemini to always return data that matches the `LLMExtractionOutput` Pydantic schema. Before I added this, I was parsing raw text with `json.loads()` and a lot of defensive try-except blocks. The model would occasionally add markdown code fences or slightly wrong key names, and the parser would crash. `with_structured_output` eliminated every one of those failures — the model now returns a typed Python object, not a string."

**During development:**

> "I used GitHub Copilot to scaffold the initial LangGraph state machine and the Pydantic model hierarchy — the boilerplate parts where you're just translating a mental model into code. That saved probably two hours. But the work that actually moved the accuracy score was iterative prompt engineering — running the pipeline on test bills, observing wrong extractions, then adding a specific instruction to prevent each one. The prompt you see in `extractor.py` is the result of about 12–15 of those iterations. Copilot can't do that loop for you — it requires watching real failures on your actual data."

---

## Section 5 — Quick Demo (~1 min)

*Screen: Switch to browser, `http://127.0.0.1:8000/docs`*

> "Let me show you what the output looks like live."

**Steps:**
1. Expand `POST /extract-from-file` → click **Try it out**
2. Click **Choose File** → select your sample medical bill PDF
3. Click **Execute**
4. Scroll to the response body

> "In a few seconds — here's the output. `pagewise_line_items` has one entry per page of the bill. Each entry has the list of `bill_items`. Each item has a name, amount, rate, and quantity. At the bottom, `total_item_count` is the total across all pages. `is_success: true` means no errors. This is the exact JSON structure the datathon scoring system consumed."

*Scroll through a few items*

> "Every number is a proper float, every field is present — because of `safe_float` and `build_response` in `main.py` normalizing the output before it leaves the server."

---

## Closing (20 sec)

> *Switch back to camera*

> "The main lesson from this project: a multimodal LLM alone isn't reliable enough for structured extraction at competition accuracy. You need layered guardrails — schema enforcement, OCR as a second signal, specific anti-hallucination prompt clauses, and model fallbacks. Stack those on top of each other, and the accuracy gets where it needs to be. Code is linked below — thanks for watching."

---

## Tips for Recording

- **Keep VS Code font size large** (18pt minimum) so code is readable in the recording
- **Move your cursor** to each function name as you say it — helps viewers follow along
- **Pause 1 second before each new section** — easy to trim in editing and gives the viewer a breath
- **Don't read the code line by line** — explain _why_ each piece exists, not _what_ it does
- **If you stumble**, just pause and redo the sentence — don't restart the whole recording
