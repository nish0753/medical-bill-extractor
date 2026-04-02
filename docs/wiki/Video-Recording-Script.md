# Video Recording Script — Loom Walkthrough

Use this as your talking guide when recording your Loom video. Each section maps to a wiki page or a file in the repo.

**Target length:** 8–12 minutes  
**Loom mode:** Screen + Camera (face visible in corner)

---

## Pre-Recording Checklist

- [ ] Service running: `python -m uvicorn app.main:app --reload`
- [ ] Browser tab open: `http://127.0.0.1:8000/docs`
- [ ] VS Code (or editor) open with `app/extractor.py` and `app/main.py`
- [ ] Sample medical bill PDF ready to upload
- [ ] Wiki open in a second browser tab for reference
- [ ] Loom recording set to "Screen + Camera"

---

## Segment 1 — Introduction (1 min)

> *Start on camera, then switch to screen share*

**Say:**
> "Hi, I'm going to walk you through a project I built for the Bajaj Finserv Datathon, where it placed in the top 50. It's called the Medical Bill Extractor — an API that takes a raw medical bill as a PDF or image and returns perfectly structured JSON with every line item extracted. Let me show you what that looks like first, then I'll walk through how it's built."

---

## Segment 2 — Live Demo (2 min)

*Screen: Swagger UI at `http://127.0.0.1:8000/docs`*

**Steps:**
1. Hit `GET /health` → show `{"status": "healthy", "version": "1.1.0"}`
2. Expand `POST /extract-from-file` → click "Try it out"
3. Upload your sample medical bill PDF
4. Hit Execute
5. Show the response — scroll through `pagewise_line_items`

**Say:**
> "In a few seconds, a raw PDF becomes this — every charge with its name, rate, quantity, and total amount, organized by page. The `total_item_count` at the bottom is the total across all pages. This is the structured output that the datathon scoring was based on."

---

## Segment 3 — Project Structure (1 min)

*Screen: File tree in VS Code or the Home wiki page*

**Say:**
> "The project has three main files. `main.py` is the FastAPI layer — it handles routing, input validation, and response normalization. `extractor.py` is the brain — it contains the entire AI pipeline. And `schemas.py` defines the Pydantic models for the response shape. Everything else is tests, Docker config, and deployment."

---

## Segment 4 — High-Level Architecture (2 min)

*Screen: Open `docs/wiki/System-Architecture.md` — show the Component Map diagram*

**Say:**
> "At a high level, here's how all the pieces connect. A request comes in through FastAPI, which validates it and passes the raw bytes into the extraction engine. The engine runs a LangGraph state machine with four nodes — prepare pages, build batches, extract batches, and aggregate results. Gemini is the AI core, and Tesseract provides an optional text signal."

*Point to the fallback chain in the diagram:*
> "One design choice I'm proud of is the fallback model chain. During the datathon, free-tier API quotas would hit zero mid-run. Rather than failing, the pipeline automatically tries the next model in the list."

---

## Segment 5 — Pipeline Deep Dive (2 min)

*Screen: Open `app/extractor.py`, scroll to `_prepare_pages_node`*

**Say:**
> "Let me walk through the pipeline step by step. When a PDF comes in, `pdf2image` converts every page to a high-resolution image. Each image then goes through Pillow preprocessing — I convert to grayscale, boost contrast by 1.5x, and sharpen by 2x. This makes text crisp before it hits Tesseract or the model."

*Scroll to `_build_batches_node`:*
> "Pages are then grouped into batches of 2 — configurable via `BATCH_SIZE`. This controls how many page images go into a single Gemini API call, keeping us under the token limit while minimizing total API round-trips."

*Scroll to `call_gemini_api_async`, point to the prompt:*
> "Each batch is sent to Gemini as a multimodal message — images plus the OCR text as context. The prompt has some specific anti-hallucination clauses I had to add through iteration. For example, early versions would sometimes mistake a date like `04/02/2026` for a monetary amount. Adding an explicit instruction — 'Do NOT confuse IDs or dates with monetary amounts' — eliminated that error."

---

## Segment 6 — How I Used AI (1.5 min)

*Screen: Open `docs/wiki/AI-Design-Decisions.md` — show the mindmap diagram*

**Say:**
> "AI was central to this project in two ways. At runtime, Gemini does the actual extraction — it reads the image and cross-checks with OCR text as a second signal. The `with_structured_output` wrapper via LangChain means the model always returns typed data — never free-form text that I'd have to parse."

> "During development, I used Copilot to scaffold the LangGraph state machine and the Pydantic models. But the key work was iterative prompt engineering. Every time I saw a wrong extraction in the test set, I'd add a specific instruction to the prompt to prevent it. The prompt you see in `extractor.py` is the result of probably 10–15 of those iterations."

---

## Segment 7 — Key Trade-offs (1 min)

*Screen: Stay on `AI-Design-Decisions.md` or open `extractor.py`*

**Say:**
> "A few trade-off decisions worth calling out. First, I chose LangGraph over plain async functions because I needed to see exactly which step failed during debugging — LangGraph's node model makes that transparent. Second, I use both OCR and vision rather than vision alone, because some of the datathon bills were low-quality scans where one signal alone wasn't reliable enough. Third, `safe_float()` in the response layer defensively handles any formatting quirk from the model — if the model returns '1,234.56' as a string, or null, it converts gracefully to a float instead of crashing."

---

## Segment 8 — Closing (30 sec)

> *Switch back to camera*

**Say:**
> "The core insight is that multimodal LLMs alone aren't reliable enough for structured extraction at competition accuracy. You need layered guardrails — explicit anti-instructions in the prompt, schema enforcement via structured output, OCR as a secondary signal, and model fallbacks for resilience. That combination is what pushed it into the top 50. Thanks for watching — the full code is linked in the description."

---

## Time Budget

| Segment | Duration |
|---------|----------|
| Introduction | ~1 min |
| Live demo | ~2 min |
| Project structure | ~1 min |
| Architecture diagram | ~2 min |
| Pipeline walkthrough | ~2 min |
| AI usage | ~1.5 min |
| Trade-offs | ~1 min |
| Closing | ~0.5 min |
| **Total** | **~11 min** |

---

## Tips

- Speak to the diagrams — point your cursor at nodes as you explain them
- Don't try to explain every line of code — pick 3–4 key functions and explain _why_ they exist
- If you stumble, pause and re-say the sentence — Loom lets you trim pauses in edit
- The Swagger UI demo is usually the most engaging part — do it early while viewers are fresh
