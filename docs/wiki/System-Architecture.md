# System Architecture

This page explains how all parts of the Medical Bill Extractor fit together, from the client request down to the final JSON response.

---

## High-Level Component Map

```mermaid
graph TD
    subgraph Client
        C1["Browser / curl / App"]
    end

    subgraph "FastAPI Layer  (app/main.py)"
        R1["POST /extract-bill-data\n(URL input)"]
        R2["POST /extract-from-file\n(multipart upload)"]
        R3["GET /health"]
        VAL["Input Validation\nPydantic + URL scheme check"]
        NORM["Response Normalization\nbuild_response()"]
    end

    subgraph "Extraction Engine  (app/extractor.py)"
        MIME["MIME / Magic-byte\nNormalization"]
        LG["LangGraph\nState Machine"]
        PP["prepare_pages\nPDF→images via pdf2image\nPillow enhancement"]
        BB["build_batches\nChunk pages by BATCH_SIZE"]
        EB["extract_batches\nGemini API calls"]
        VA["validate_aggregate\nMerge results"]
    end

    subgraph "AI Layer"
        OCR["Tesseract OCR\n(optional text signal)"]
        GEM["Google Gemini\n2.5 Flash (primary)"]
        FB1["Fallback Model 1\ngemini-flash-latest"]
        FB2["Fallback Model 2\ngemini-2.0-flash"]
    end

    subgraph "Schemas  (app/schemas.py)"
        SC["Pydantic Models\nFinalResponse\nDataBlock\nPageLineItems\nLineItem"]
    end

    C1 --> R1
    C1 --> R2
    C1 --> R3
    R1 --> VAL
    R2 --> VAL
    VAL --> MIME
    MIME --> LG
    LG --> PP
    PP --> OCR
    PP --> BB
    BB --> EB
    EB --> GEM
    GEM -- "quota / 429" --> FB1
    FB1 -- "quota / 429" --> FB2
    GEM --> VA
    FB1 --> VA
    FB2 --> VA
    VA --> NORM
    NORM --> SC
    SC --> C1

    style LG fill:#e8f5e9,stroke:#2e7d32
    style GEM fill:#fff3e0,stroke:#e65100
    style OCR fill:#e3f2fd,stroke:#1565c0
    style SC fill:#f3e5f5,stroke:#6a1b9a
```

---

## Layer Descriptions

### 1. FastAPI Layer (`app/main.py`)

The entry point. Responsible for:
- **URL input validation** — only `http://` and `https://` schemes accepted, host must be present
- **File size enforcement** — rejects uploads over 20 MB
- **Thread isolation** — synchronous I/O (download, OCR, PDF conversion) is dispatched to a thread-pool executor so the async event loop is never blocked
- **Response normalization** — `build_response()` applies `safe_float()` to every numeric field and coerces all values to the exact schema shape

### 2. Extraction Engine (`app/extractor.py`)

The brain. A **LangGraph state machine** with four sequential nodes:

| Node | Responsibility |
|------|----------------|
| `prepare_pages` | Converts PDFs to page images; applies Pillow grayscale + contrast + sharpness; runs optional OCR |
| `build_batches` | Chunks page list into groups of `BATCH_SIZE` (default 2) |
| `extract_batches` | Sends each batch to Gemini with a structured prompt |
| `validate_aggregate` | Merges all batch results; recomputes `total_item_count` |

### 3. AI Layer

Two parallel signals flow into Gemini:
1. **Visual signal** — base64-encoded JPEG of each page
2. **Text signal** — raw OCR output from Tesseract (when available)

Gemini uses `with_structured_output(LLMExtractionOutput)` via LangChain, which means it _always_ returns typed, validated data — never free-form text.

### 4. Schemas (`app/schemas.py`)

Pure Pydantic models that define the wire format. Nothing is typed as `Any` in the public response — every field is explicit.

---

## Request Lifecycle (Sequence Diagram)

```mermaid
sequenceDiagram
    participant Client
    participant FastAPI
    participant Extractor
    participant Tesseract
    participant Gemini

    Client->>FastAPI: POST /extract-from-file (PDF bytes)
    FastAPI->>FastAPI: Validate size ≤ 20MB
    FastAPI->>Extractor: extract_bill_data(bytes, mime_type)
    Extractor->>Extractor: Detect MIME / magic bytes

    Extractor->>Extractor: LangGraph: prepare_pages
    Extractor->>Tesseract: image_to_string(page_image)
    Tesseract-->>Extractor: raw OCR text

    Extractor->>Extractor: LangGraph: build_batches
    Extractor->>Extractor: LangGraph: extract_batches

    loop For each batch
        Extractor->>Gemini: [images + OCR text + prompt]
        alt Success
            Gemini-->>Extractor: LLMExtractionOutput (typed JSON)
        else 429 Quota Error
            Extractor->>Extractor: Exponential backoff, retry
            Extractor->>Gemini: retry with same or fallback model
        end
    end

    Extractor->>Extractor: LangGraph: validate_aggregate
    Extractor-->>FastAPI: {pagewise_line_items, total_item_count}
    FastAPI->>FastAPI: build_response() — normalize all fields
    FastAPI-->>Client: FinalResponse JSON
```

---

## Technology Map

| Layer | Technology | Why |
|-------|-----------|-----|
| API | FastAPI + Pydantic | Async, type-safe, auto-generates Swagger docs |
| Orchestration | LangGraph | Typed state machine — easy to add/reorder steps |
| LLM integration | LangChain + ChatGoogleGenerativeAI | `with_structured_output` eliminates JSON parsing errors |
| LLM | Google Gemini 2.5 Flash | Best accuracy/speed/cost ratio for multimodal extraction |
| PDF handling | pdf2image (poppler) | Reliable PDF-to-image conversion at configurable DPI |
| Image enhancement | Pillow | Grayscale + contrast + sharpness boost before OCR |
| OCR | pytesseract (Tesseract) | Free text signal to help the LLM with blurry scans |
| Deployment | Docker + Render | `render.yaml` enables one-click cloud deployment |
| Testing | pytest | Unit, API, and integration test layers |
