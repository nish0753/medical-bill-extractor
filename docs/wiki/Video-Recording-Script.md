# Video Recording Script — Detailed Section-by-Section Guide

Every line below is a cue, a camera instruction, or a word-for-word sentence you can say out loud. Nothing is left vague — each sub-beat tells you exactly what is on screen, where your cursor is, and what you're saying.

**Target length:** 8–10 minutes  
**Recording setup:** Screen share + camera bubble in corner  
**Tone:** Conversational, confident — talk to the camera like you're explaining to a smart friend, not reading a report

---

## Pre-Recording Setup Checklist

Before you hit record, confirm all of these:

- [ ] Terminal: `python -m uvicorn app.main:app --reload` is running, no errors
- [ ] Browser tab 1: `http://127.0.0.1:8000/docs` (Swagger UI) — keep minimised until Section 5
- [ ] VS Code: `app/main.py` open in left pane, `app/extractor.py` open in right pane
- [ ] VS Code font size: **18pt or larger** — viewers watch at small sizes
- [ ] `app/schemas.py` open in a third tab in VS Code
- [ ] Sample PDF bill: downloaded and sitting on your Desktop, ready to pick in a file dialog
- [ ] `.env` has a valid `GOOGLE_API_KEY`
- [ ] All Slack / notification popups silenced

---

## Time Budget

| Section | Core content | Target |
|---------|-------------|--------|
| 1 — Open the project | Problem + repo tour | 1 min 30 sec |
| 2 — Walk through the code | `main.py` → `extractor.py` → `schemas.py` | 3 min 30 sec |
| 3 — Design & trade-offs | 3 architectural decisions | 2 min |
| 4 — How I used AI | Runtime + dev-time AI | 1 min 30 sec |
| 5 — Quick demo | Live Swagger UI call | 1 min |
| Closing | One-line takeaway | 20 sec |
| **Total** | | **~9 min 50 sec** |

---

---

# SECTION 1 — Open the Project
### Target: 1 min 30 sec | Start: on camera

---

### Beat 1 — Hook on camera (0:00 – 0:20)

> *Face camera directly. Speak at a calm pace. No screen share yet.*

**Say:**

> "Hi — I want to walk you through a project I built for the Bajaj Finserv Health Datathon. It placed in the **top 50** out of several hundred teams. The problem it solves is one of those things that sounds simple until you actually try it: take any medical bill — a PDF, a scanned image, a phone photo — and automatically extract every single line item from it as structured data."

---

### Beat 2 — State the problem clearly (0:20 – 0:40)

> *Still on camera*

**Say:**

> "The challenge is that medical bills have no standard format. One hospital has a table with ten columns. Another is a handwritten receipt. Another is a three-page PDF where charges are spread across pages. You can't template-match your way out of this — you need something that actually understands the document. That's what this API does."

---

### Beat 3 — Switch to VS Code, show file tree (0:40 – 1:30)

> *Switch to screen share. Open VS Code Explorer panel. The `app/` folder should be visible.*

**Say:**

> "Let me show you the repo first so you know what you're looking at."

*Hover cursor over the `app/` folder*

> "The entire application is three files in this `app` folder."

*Click to open `main.py` — let it appear on screen*

> "`main.py` — this is the FastAPI layer. It takes in the HTTP request, validates it, calls the extraction engine, and shapes the response into the exact format the scoring system expected."

*Click to open `extractor.py`*

> "`extractor.py` — this is where all the AI work happens. PDF conversion, image preprocessing, OCR, the LangGraph state machine, and the Gemini API calls. Everything that makes this more than just a wrapper lives here."

*Click to open `schemas.py`*

> "`schemas.py` — the Pydantic response contract. Every field in the public API response is typed explicitly here. Nothing returns as a generic object or a plain dict."

*Move cursor to show `tests/`, `Dockerfile`, `render.yaml`*

> "Outside the `app` folder there's a test suite, a Dockerfile for containerized deployment, and a `render.yaml` for one-click cloud hosting. But the product is those three files."

---

---

# SECTION 2 — Walk Through the Code
### Target: 3 min 30 sec | Screen: VS Code

---

## Part 2A — `main.py`: The API Layer
### Target: ~1 min 10 sec

---

### Beat 4 — App setup and CORS (1:30 – 1:45)

> *Open `app/main.py`. Scroll to the top, lines 24–37 — the `FastAPI(...)` constructor and `add_middleware`.*

**Say:**

> "Starting at the top of `main.py`. The app is a standard FastAPI instance. I've added CORS middleware with `allow_origins=["*"]` — that's wide open. For a competition API that's fine. In production you'd lock this down to specific domains."

---

### Beat 5 — `BillRequest` validator (1:45 – 2:10)

> *Scroll to lines 55–67 — the `BillRequest` class and `validate_url` method.*

**Say:**

> "Here's the first real decision. When someone sends a URL to `/extract-bill-data`, this Pydantic validator runs **before** anything else. It checks that the URL scheme is `http` or `https` — see line 63, `parsed.scheme not in ALLOWED_URL_SCHEMES`. If someone sends `ftp://` or a plain string without a scheme, they get a 422 back immediately. The AI pipeline never even starts. This is a security boundary — you don't want the server fetching arbitrary internal URLs."

---

### Beat 6 — `safe_float` and `build_response` (2:10 – 2:40)

> *Scroll up slightly to lines 75–120 — `safe_float`, `_normalize_item`, `_normalize_page`, `build_response`.*

**Say:**

> "Skip down a bit to `safe_float` — line 75. This tiny function is doing real work. After the AI pipeline returns data, every single numeric field — `item_amount`, `item_rate`, `item_quantity` — passes through this function before it goes in the response. Why? Because the model sometimes returns a number as a string with commas: `'1,234.56'`. Or it returns `null`. `safe_float` handles all of that — strips commas, handles null, and returns a proper Python float. Without this, the API would crash on edge cases."

*Point to `build_response` at line 107*

> "`build_response` calls this for every item on every page. The result is that no matter how messy the model output is, what leaves the server is always clean, typed, and valid."

---

## Part 2B — `extractor.py`: The AI Pipeline
### Target: ~1 min 45 sec

---

### Beat 7 — The LangGraph state machine (2:40 – 3:05)

> *Switch to `extractor.py`. Scroll to `_get_workflow` at lines 237–252.*

**Say:**

> "Now the brain. The pipeline is a **LangGraph state machine**. Scroll down to `_get_workflow` — this is where the graph is built. Four nodes: `prepare_pages`, `build_batches`, `extract_batches`, `validate_aggregate`. Each one wired to the next with `add_edge`. This is a directed acyclic graph — data only flows forward, one node at a time."

*Point to `BillExtractionState` TypedDict at line 64*

> "The state object — `BillExtractionState` up at line 64 — is a typed dictionary that all four nodes share. Each node reads what it needs and writes only its own keys back. This isolation is what makes debugging so much easier — if something goes wrong, I know exactly which node produced the bad data."

---

### Beat 8 — Node 1: Prepare Pages (3:05 – 3:25)

> *Scroll to `_prepare_pages_node` at lines 128–185.*

**Say:**

> "First node — `_prepare_pages_node`. For a PDF, it calls `pdf2image` to convert every page to a 200 DPI JPEG. For a raw image, it calls `preprocess_image`."

*Scroll to `preprocess_image` at lines 280–299*

> "Here's `preprocess_image`. Three steps: convert to grayscale, boost contrast by 1.5×, boost sharpness by 2×. This is Pillow doing the work. On low-quality scanned bills, this preprocessing measurably improved the model's accuracy — the text edges become sharper and the model reads them more reliably."

---

### Beat 9 — Node 2: Build Batches + Node 3: Extract with Gemini (3:25 – 4:00)

> *Scroll to `_build_batches_node` at lines 188–198.*

**Say:**

> "Second node: `_build_batches_node`. Pages are grouped into batches of two — controlled by the `BATCH_SIZE` environment variable. Two pages per API call keeps us under the token limit while halving the number of round-trips compared to one page at a time."

*Scroll to `call_gemini_api_async` at lines 341 onwards. Stop at the `base_prompt` string, lines 352–379.*

> "Third node calls Gemini. Here's the prompt. Let me call out a few specific lines that weren't in the first version."

*Highlight instruction 1 — line 359*

> "Instruction one: 'Do NOT confuse IDs or Dates with monetary amounts.' Early runs would see a date like `04/02/2026` in a patient ID column and extract `2026.0` as a charge. This one line fixed that entirely."

*Highlight instruction 2 — line 360*

> "Instruction two: 'Do NOT extract Sub-total, Total, Tax, or Discount lines as items.' Without this, the model would correctly find all the line items and then also add the final total as an extra item — doubling the total amount. Each of these clauses is a real failure mode I observed on real bills."

*Point to `ocr_context` appended at line 381*

> "After the prompt, if OCR text is available, it's appended as context. So the model gets both the visual image and the raw OCR text in the same call."

---

### Beat 10 — Node 4: Aggregate (4:00 – 4:15)

> *Scroll to `_validate_and_aggregate_node` at lines 216–233.*

**Say:**

> "Last node: aggregate. It collects results from all batches and builds the final page list. Notice that `total_item_count` is recomputed here by actually counting the items in the list — not by trusting the model's self-reported count, which was wrong about 15% of the time on the test set."

---

## Part 2C — `schemas.py`: The Response Contract
### Target: ~35 sec

---

### Beat 11 — Schema walkthrough (4:15 – 4:50)

> *Switch to `schemas.py`. It's short — the whole file is visible at once.*

**Say:**

> "Finally, `schemas.py`. Read it top to bottom and you immediately know what the API returns. `LineItem` is the atomic unit: `item_name`, `item_amount`, `item_rate`, `item_quantity`. `PageLineItems` groups items by page. `DataBlock` holds all pages plus the total count. `FinalResponse` wraps everything with `is_success`, `token_usage`, and an optional `error` field. This is the contract. The scoring system parsed this JSON directly, so every field name here had to be exact."

---

---

# SECTION 3 — High-Level Design & Trade-offs
### Target: 2 min | Screen: VS Code or Architecture doc

---

### Beat 12 — Frame the section (4:50 – 5:00)

> *Stay on VS Code or open `docs/wiki/System-Architecture.md`*

**Say:**

> "Let me walk through three architectural decisions that I made consciously — and what I was trading off."

---

### Beat 13 — Trade-off 1: LangGraph vs. plain functions (5:00 – 5:30)

> *Scroll back to `_get_workflow` in `extractor.py` if not already there*

**Say:**

> "**Trade-off one: LangGraph versus a chain of async function calls.**"

> "I could have written this as `result_a = await prepare(); result_b = await batch(result_a)` and so on. That would be fewer lines of code. I chose LangGraph because it gives me named, independent nodes with logged state at each boundary. When a bill came out wrong in testing, I could look at the logs and say: 'the pipeline produced this state after `build_batches` — so the bug is in `extract_batches`, not in `prepare_pages`.' That precision cut my debugging time in half. The cost is a bit of boilerplate to define the graph. Worth it."

---

### Beat 14 — Trade-off 2: OCR + Vision vs. Vision alone (5:30 – 6:00)

> *Scroll to `_prepare_pages_node`, point to the `page_ocr_context` lines*

**Say:**

> "**Trade-off two: dual-signal extraction — OCR plus vision — versus vision alone.**"

> "Gemini is a multimodal model. In theory you just give it the image and it reads the text. In practice, the datathon had bills that were phone photos of paper bills — slightly blurry, sometimes tilted. The model would misread individual digits. By also running Tesseract OCR on every page and including the raw OCR text in the prompt, I gave the model a second signal to reconcile against. When the image says `1,200` and the OCR text says `1,200`, the model is confident. When they disagree, the model usually picks the one that makes more sense in context. The cost is extra compute for the OCR step. The benefit was a measurable accuracy improvement on noisy documents."

---

### Beat 15 — Trade-off 3: Model fallback chain (6:00 – 6:30)

> *Scroll to `_candidate_models` at lines 73–79 and the retry loop in `call_gemini_api_async`*

**Say:**

> "**Trade-off three: the fallback model chain.**"

> "The datathon rules allowed free-tier Gemini API keys. Free tier means quota limits. A single model would hit zero mid-run and throw a `ResourceExhausted` error. My solution: `_candidate_models()` returns an ordered list — primary model first, then fallbacks. The retry loop checks: if the error is a quota-zero error for the current model, break out of retries and move to the next model in the list. If it's a regular quota exceeded — like a rate limit that will reset — it does exponential backoff and retries on the same model. This meant the pipeline could run through hundreds of documents without manual intervention. The trade-off is slightly more complex error-handling logic. But for a competition setting, resilience matters more than simplicity."

---

---

# SECTION 4 — How I Used AI While Building
### Target: 1 min 30 sec | Screen: `extractor.py`

---

### Beat 16 — Frame the two ways AI was used (6:30 – 6:40)

> *Open `extractor.py`, scroll to `_get_llm` function at lines 82–95*

**Say:**

> "AI was involved in this project in two completely different ways — as the runtime extraction engine, and as a development tool. Let me cover both."

---

### Beat 17 — Runtime AI: `with_structured_output` (6:40 – 7:15)

> *Point to line 94 — `.with_structured_output(LLMExtractionOutput)`*

**Say:**

> "At runtime, the key technique is this one line: `.with_structured_output(LLMExtractionOutput)`. This is a LangChain feature that wraps the Gemini model with a schema constraint. When I call `llm.ainvoke(...)`, the response I get back is not a string — it's an already-validated Python object of type `LLMExtractionOutput`, with bill items already in the right structure."

> "Before I used this, I was calling the model, getting a string, and doing `json.loads()` on it. The model would sometimes wrap the output in markdown code fences like triple-backtick JSON. Or it would use slightly wrong key names. Every one of those cases caused a `KeyError` or a `JSONDecodeError`. Switching to `with_structured_output` eliminated all of those — the model is now constrained to produce valid schema-matching output. It doesn't get a choice about the format."

---

### Beat 18 — Dev-time AI: Copilot + prompt iteration (7:15 – 8:00)

> *Scroll to the `base_prompt` string at lines 352–379*

**Say:**

> "During development, I used GitHub Copilot for the structural boilerplate — setting up the LangGraph state machine, writing the Pydantic model hierarchy, writing the FastAPI route handlers. That's the kind of work where you know exactly what you want but you're just translating a mental model into code. Copilot is fast at that."

> "But the work that actually moved the accuracy metric was something Copilot can't do: **iterative prompt engineering on real data**. Every clause you see in this prompt came from watching a failure. I'd run the pipeline on twenty test bills, see that it extracted a date as a charge, then add the instruction 'Do NOT confuse IDs or Dates with monetary amounts.' Run again. See the next failure. Add another instruction. The prompt you're looking at is the result of about twelve to fifteen of those iterations."

> "The insight is that AI-assisted development gives you speed on structure, but domain accuracy — the part that actually determines your score — still requires you to observe real failures on your own data and fix them yourself."

---

---

# SECTION 5 — Quick Demo
### Target: 1 min | Screen: Browser → Swagger UI

---

### Beat 19 — Switch to browser (8:00 – 8:05)

> *Switch to browser tab with `http://127.0.0.1:8000/docs`*

**Say:**

> "Let me show you a live call. I've got the server running locally."

---

### Beat 20 — Health check (8:05 – 8:15)

> *Expand `GET /health` in Swagger UI. Click "Try it out" → "Execute".*

**Say:**

> "Quick health check first — `GET /health`. You can see `status: healthy`, `version: 1.1.0`. The service is up."

---

### Beat 21 — File upload and execution (8:15 – 8:40)

> *Expand `POST /extract-from-file`. Click "Try it out".*

**Say:**

> "Now `POST /extract-from-file`. I'll click 'Try it out', choose my sample medical bill PDF…"

*Click "Choose File", navigate to your sample PDF, select it*

> "…and hit Execute."

*Click Execute. Wait for response — it will take 3–6 seconds.*

> "The server is converting the PDF, running preprocessing, batching pages, calling Gemini — all of that is happening right now."

---

### Beat 22 — Walk through the response (8:40 – 9:20)

> *Response appears. Scroll slowly through it.*

**Say:**

> "Here's the response. `is_success: true`. Now look at `data`."

*Point to `pagewise_line_items`*

> "`pagewise_line_items` — one object per page. This bill has one page, so one entry."

*Expand into `bill_items`*

> "Inside, `bill_items`. Each item has `item_name`, `item_amount`, `item_rate`, `item_quantity`. Look at the amounts — these are clean floats, not strings, not nulls."

*Scroll to `total_item_count` at the bottom*

> "`total_item_count` at the bottom — that's the count of items across all pages. This number is what the datathon scoring system used to measure extraction accuracy."

> "The whole call took under 5 seconds for a single-page bill. Multi-page bills scale linearly — two batches take about twice as long."

---

---

# CLOSING
### Target: 20 sec | Camera

---

### Beat 23 — One-line takeaway (9:20 – 9:40)

> *Switch back to camera*

**Say:**

> "The core lesson: a multimodal LLM alone is not reliable enough for structured extraction at competition accuracy. You need layers — schema enforcement so the output shape is always correct, OCR as a second signal for noisy documents, specific anti-hallucination clauses in the prompt for each failure mode you've observed, and model fallbacks so quotas don't kill your run. Stack those together and you get something that actually works. The full code is linked in the description — thanks for watching."

---

---

## Transition Phrases (Use Between Sections)

These help you move between sections without awkward silence:

- End of Section 1 → Section 2: *"Okay, let me open the code and walk through it properly."*
- End of Section 2A → 2B: *"That's the API layer. Now let me show you where the actual intelligence lives."*
- End of Section 2B → 2C: *"One more file — schemas — and then we'll talk architecture."*
- End of Section 2 → Section 3: *"Now that you've seen the code, let me explain why it's structured this way."*
- End of Section 3 → Section 4: *"Those were the architectural decisions. Let me now specifically talk about how AI played a role."*
- End of Section 4 → Section 5: *"Enough theory — let me just show you it working."*

---

## Recording Tips

- **Font size:** 18pt minimum in VS Code. Viewers often watch at 720p.
- **Cursor as pointer:** Every time you mention a function name, hover your cursor over it. This is the equivalent of pointing at a whiteboard.
- **Don't apologise for pauses.** If you lose your place, just pause, look at the next beat, and continue. Pauses are easy to cut.
- **Speed:** Speak slower than you think you need to. Technical content at normal conversational speed is too fast.
- **One idea per sentence.** If a sentence has more than one technical concept, split it into two sentences.
- **The demo is your strongest moment.** Go slowly through the response JSON — viewers will pause and read it themselves if you give them time.
