import os
import requests
import io
import gc
import shutil
import logging
import asyncio
import base64
import re
import time
from typing import Any, TypedDict
from urllib.parse import urlparse

from dotenv import load_dotenv
from PIL import Image, ImageEnhance
from pdf2image import convert_from_bytes
from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import StateGraph, END

from . import tracing

try:
    import pytesseract as _pytesseract
except ImportError:
    _pytesseract = None

load_dotenv(override=True)
tracing.setup()

# --- Logging ---
logger = logging.getLogger(__name__)

# --- Configuration ---
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "2"))
PDF_DPI = int(os.getenv("PDF_DPI", "200"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "10"))
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
USE_MOCK_MODE = os.getenv("USE_MOCK_MODE", "false").lower() == "true"
GEMINI_MODEL_FALLBACKS = [
    model.strip()
    for model in os.getenv("GEMINI_MODEL_FALLBACKS", "gemini-flash-latest,gemini-2.0-flash").split(",")
    if model.strip()
]

# Create model clients once (reuse across calls)
_llm_cache: dict[tuple[str, str], ChatGoogleGenerativeAI] = {}
_workflow = None


class LLMLineItem(BaseModel):
    item_name: str = Field(default="")
    item_amount: float = Field(default=0.0)
    item_rate: float = Field(default=0.0)
    item_quantity: float = Field(default=0.0)


class LLMPageLineItems(BaseModel):
    page_no: str = Field(default="1")
    page_type: str = Field(default="Bill Detail")
    bill_items: list[LLMLineItem] = Field(default_factory=list)


class LLMExtractionOutput(BaseModel):
    pagewise_line_items: list[LLMPageLineItems] = Field(default_factory=list)
    total_item_count: int = Field(default=0)


class BillExtractionState(TypedDict, total=False):
    file_data: bytes
    mime_type: str
    pages: list[dict[str, Any]]
    batches: list[dict[str, Any]]
    batch_results: list[dict[str, Any]]
    result: dict[str, Any]


def _candidate_models() -> list[str]:
    ordered = [GEMINI_MODEL, *GEMINI_MODEL_FALLBACKS]
    deduped: list[str] = []
    for model in ordered:
        if model not in deduped:
            deduped.append(model)
    return deduped


def _get_llm(model_name: str) -> ChatGoogleGenerativeAI:
    api_key = (os.getenv("GOOGLE_API_KEY") or "").strip().strip('"').strip("'")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY not found in environment variables.")

    current_config = (api_key, model_name)
    if current_config not in _llm_cache:
        _llm_cache[current_config] = ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=api_key,
            temperature=0,
            max_retries=0,
        ).with_structured_output(LLMExtractionOutput)
    return _llm_cache[current_config]


async def _get_mock_response() -> dict[str, Any]:
    """Return demo/mock bill extraction data for testing without API calls."""
    return {
        "pagewise_line_items": [
            {
                "page_no": "1",
                "page_type": "Bill Detail",
                "bill_items": [
                    {"item_name": "Medical Consultation", "item_amount": 500.0, "item_rate": 500.0, "item_quantity": 1.0},
                    {"item_name": "Lab Tests", "item_amount": 1200.0, "item_rate": 1200.0, "item_quantity": 1.0},
                    {"item_name": "Medicines", "item_amount": 450.0, "item_rate": 450.0, "item_quantity": 1.0},
                ]
            }
        ],
        "total_item_count": 3
    }


def _image_summary(inputs: dict) -> dict:
    image = inputs.get("image")
    return {"image_size": list(getattr(image, "size", ())), "mode": getattr(image, "mode", None)}


def _text_summary(text: Any) -> dict:
    # The OCR text itself is never traced (patient details); only how much was read.
    return {"chars": len(text or ""), "lines": len((text or "").splitlines())}


@tracing.traceable("ocr_page", run_type="tool", inputs=_image_summary, outputs=_text_summary)
async def _extract_ocr_text(image: Image.Image) -> str:
    if not pytesseract:
        return ""

    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, pytesseract.image_to_string, image)
    except Exception as exc:
        logger.warning("OCR Warning: %s", exc)
        return ""


@tracing.traceable("render_pdf_pages", run_type="tool",
                   inputs=lambda i: {"pdf_bytes": len(i.get("file_data") or b""), "dpi": PDF_DPI},
                   outputs=lambda images: {"page_count": len(images),
                                           "page_sizes": [list(im.size) for im in images[:5]]})
async def _render_pdf(file_data: bytes) -> list:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: convert_from_bytes(file_data, dpi=PDF_DPI))


@tracing.traceable("preprocess_image", run_type="tool",
                   inputs=lambda i: {"image_bytes": len(i.get("file_data") or b"")},
                   outputs=lambda out: {"jpeg_bytes": len(out or b"")})
async def _preprocess(file_data: bytes) -> bytes:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, preprocess_image, file_data)


@tracing.traceable("download_document", run_type="tool",
                   inputs=lambda i: {"host": urlparse(i.get("url", "")).netloc},
                   outputs=lambda out: {"bytes": len(out[0]), "content_type": out[1]})
async def _download(url: str) -> tuple:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, download_image, url)


async def _prepare_pages_node(state: BillExtractionState) -> dict[str, Any]:
    file_data = state["file_data"]
    mime_type = state["mime_type"]

    if mime_type == "application/pdf":
        logger.info("PDF detected. Converting to images for batch processing.")
        images = await _render_pdf(file_data)
        logger.info("PDF converted. Total pages: %d", len(images))

        pages = []
        for page_idx, image in enumerate(images, start=1):
            page_ocr_text = await _extract_ocr_text(image)
            page_ocr_context = ""
            if page_ocr_text:
                page_ocr_context = f"\n--- Page {page_idx} Raw OCR Text ---\n{page_ocr_text}\n"

            with io.BytesIO() as output:
                image.save(output, format="JPEG", quality=95)
                pages.append(
                    {
                        "page_no": page_idx,
                        "media_payload": ("image/jpeg", output.getvalue()),
                        "ocr_context": page_ocr_context,
                    }
                )
            image.close()

        gc.collect()
        return {"pages": pages}

    if mime_type.startswith("image/"):
        logger.info("Applying image preprocessing...")
        processed_data = await _preprocess(file_data)

        page_ocr_context = ""
        if pytesseract:
            try:
                image = Image.open(io.BytesIO(processed_data))
                page_ocr_text = await _extract_ocr_text(image)
                image.close()
                if page_ocr_text:
                    page_ocr_context = f"\n--- Page 1 Raw OCR Text ---\n{page_ocr_text}\n"
            except Exception as exc:
                logger.warning("OCR Warning: %s", exc)

        return {
            "pages": [
                {
                    "page_no": 1,
                    "media_payload": ("image/jpeg", processed_data),
                    "ocr_context": page_ocr_context,
                }
            ]
        }

    raise ValueError(f"Unsupported mime type for extraction: {mime_type}")


async def _build_batches_node(state: BillExtractionState) -> dict[str, Any]:
    pages = state.get("pages", [])
    batches = []

    for i in range(0, len(pages), BATCH_SIZE):
        page_slice = pages[i : i + BATCH_SIZE]
        media_payloads = [page["media_payload"] for page in page_slice]
        ocr_context = "".join(page["ocr_context"] for page in page_slice if page.get("ocr_context"))
        page_nos = [int(page["page_no"]) for page in page_slice]
        batches.append({"media_payloads": media_payloads, "ocr_context": ocr_context, "page_nos": page_nos})

    return {"batches": batches}


def _resolve_page_no(model_page_no: Any, batch_page_nos: list[int], position: int) -> int:
    """Map the model's page number for one batch onto the physical page in the file.

    The model only sees its own batch, so it usually numbers pages from 1 (relative).
    It may also echo the physical number from the OCR headers (absolute). Both are
    accepted; anything else falls back to the entry's position within the batch.
    The two readings never conflict: relative 1..k overlaps absolute numbers only in
    the first batch, where they are identical.
    """
    count = len(batch_page_nos)
    if count == 1:
        return batch_page_nos[0]

    match = re.search(r"\d+", str(model_page_no or ""))
    if match:
        n = int(match.group())
        if n in batch_page_nos:
            return n
        if 1 <= n <= count:
            return batch_page_nos[n - 1]

    return batch_page_nos[min(position, count - 1)]


async def _extract_batches_node(state: BillExtractionState) -> dict[str, Any]:
    batches = state.get("batches", [])
    batch_results = []

    for idx, batch in enumerate(batches, start=1):
        logger.info("Sending batch %d/%d with %d page(s) to Gemini...", idx, len(batches), len(batch["media_payloads"]))
        batch_result = await call_gemini_api_async(batch["media_payloads"], batch.get("ocr_context", ""))
        batch_results.append(batch_result)

        if idx < len(batches):
            await asyncio.sleep(1)

    return {"batch_results": batch_results}


async def _validate_and_aggregate_node(state: BillExtractionState) -> dict[str, Any]:
    batches = state.get("batches", [])
    pages_by_no: dict[int, dict[str, Any]] = {}

    for batch_idx, batch_result in enumerate(state.get("batch_results", [])):
        pages = batch_result.get("pagewise_line_items", []) if isinstance(batch_result, dict) else []
        if not isinstance(pages, list):
            continue
        batch_page_nos = batches[batch_idx].get("page_nos") if batch_idx < len(batches) else None
        if not batch_page_nos:
            batch_page_nos = [1]

        batch_pages = [p for p in pages if isinstance(p, dict)]
        resolved = [_resolve_page_no(p.get("page_no"), batch_page_nos, i) for i, p in enumerate(batch_pages)]
        if len(batch_pages) == len(batch_page_nos) and len(set(resolved)) < len(resolved):
            # One entry per page but clashing labels (e.g. all "1"): trust the order instead.
            resolved = list(batch_page_nos)

        for page, physical_no in zip(batch_pages, resolved):
            items = page.get("bill_items", [])
            items = items if isinstance(items, list) else []
            if physical_no in pages_by_no:
                # The model split one page into several entries: merge them.
                pages_by_no[physical_no]["bill_items"].extend(items)
            else:
                pages_by_no[physical_no] = {**page, "page_no": str(physical_no), "bill_items": list(items)}

    aggregated_pages = [pages_by_no[no] for no in sorted(pages_by_no)]

    total_item_count = sum(
        len(page.get("bill_items", []))
        for page in aggregated_pages
        if isinstance(page.get("bill_items", []), list)
    )

    return {
        "result": {
            "pagewise_line_items": aggregated_pages,
            "total_item_count": total_item_count,
        }
    }


def _get_workflow():
    global _workflow
    if _workflow is None:
        workflow = StateGraph(BillExtractionState)
        workflow.add_node("prepare_pages", _prepare_pages_node)
        workflow.add_node("build_batches", _build_batches_node)
        workflow.add_node("extract_batches", _extract_batches_node)
        workflow.add_node("validate_aggregate", _validate_and_aggregate_node)

        workflow.set_entry_point("prepare_pages")
        workflow.add_edge("prepare_pages", "build_batches")
        workflow.add_edge("build_batches", "extract_batches")
        workflow.add_edge("extract_batches", "validate_aggregate")
        workflow.add_edge("validate_aggregate", END)
        _workflow = workflow.compile()

    return _workflow

# --- Tesseract Setup ---
def _setup_tesseract():
    """Configure tesseract path once at startup."""
    if _pytesseract is None:
        logger.warning("pytesseract not installed. OCR step will be skipped.")
        return None

    tesseract_path = shutil.which("tesseract")
    if tesseract_path:
        logger.info("Tesseract found at: %s", tesseract_path)
        _pytesseract.pytesseract.tesseract_cmd = tesseract_path
        return _pytesseract

    common_paths = ["/usr/bin/tesseract", "/usr/local/bin/tesseract", "/bin/tesseract"]
    for p in common_paths:
        if os.path.exists(p):
            logger.info("Tesseract found at: %s", p)
            _pytesseract.pytesseract.tesseract_cmd = p
            return _pytesseract

    logger.warning("Tesseract binary NOT found. OCR step will be skipped.")
    return None

pytesseract = _setup_tesseract()

def preprocess_image(image_bytes: bytes) -> bytes:
    """
    Enhance image quality for better OCR/Extraction.
    - Grayscale
    - Contrast Enhancement
    - Sharpness Enhancement
    """
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image = image.convert('L')
        enhancer = ImageEnhance.Contrast(image)
        image = enhancer.enhance(1.5)
        enhancer = ImageEnhance.Sharpness(image)
        image = enhancer.enhance(2.0)
        output_buffer = io.BytesIO()
        image.save(output_buffer, format='JPEG', quality=95)
        return output_buffer.getvalue()
    except Exception as e:
        logger.warning("Image preprocessing failed: %s", e)
        return image_bytes

def download_image(url: str) -> bytes:
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        content_type = response.headers.get('Content-Type', '')
        if not content_type.startswith('image/') and content_type != 'application/pdf':
            raise ValueError(f"Unsupported content type: {content_type}")
        return response.content, content_type
    except Exception as e:
        logger.error("Error downloading image: %s", e)
        raise

async def extract_bill_data(image_source, mime_type: str = None) -> dict:
    api_key = (os.getenv("GOOGLE_API_KEY") or "").strip().strip('"').strip("'")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY not found in environment variables.")

    # 1. Get Raw Data
    if isinstance(image_source, str):
        file_data, mime_type = await _download(image_source)
    else:
        file_data = image_source
        if not mime_type:
            raise ValueError("Mime type must be provided for file uploads.")

    normalized_mime_type = (mime_type or "").split(";", 1)[0].strip().lower()
    mime_type = normalized_mime_type or mime_type

    # 2. Magic Byte Detection (Fix for application/octet-stream)
    if file_data and len(file_data) > 4:
        if file_data[:4] == b'%PDF':
            logger.debug("Detected PDF magic bytes. Overriding mime_type '%s' to 'application/pdf'", mime_type)
            mime_type = 'application/pdf'

    with tracing.span("extract_bill", inputs={"mime_type": mime_type, "size_bytes": len(file_data or b"")},
                      mime_type=mime_type, size_bytes=len(file_data or b""), batch_size=BATCH_SIZE,
                      ocr_available=pytesseract is not None) as sp:
        started = time.monotonic()
        workflow = _get_workflow()
        final_state = await workflow.ainvoke({"file_data": file_data, "mime_type": mime_type})
        result = final_state.get("result", {"pagewise_line_items": [], "total_item_count": 0})
        sp.set(page_count=len(final_state.get("pages", [])), batch_count=len(final_state.get("batches", [])),
               item_count=result.get("total_item_count", 0), seconds=round(time.monotonic() - started, 2))
        sp.output({"pages_returned": len(result.get("pagewise_line_items", [])),
                   "total_item_count": result.get("total_item_count", 0)})
        return result


async def call_gemini_api_async(media_payloads: list[tuple[str, bytes]], ocr_context: str = "") -> dict[str, Any]:
    """Call Gemini through LangChain with media payloads and optional OCR context.
    Returns result_dict with extracted data."""
    
    # Return mock data if in demo mode (useful when API quota is exhausted)
    if USE_MOCK_MODE:
        logger.warning("MOCK MODE ENABLED: Returning sample bill data. Set USE_MOCK_MODE=false to use real API.")
        return await _get_mock_response()
    
    model_errors: list[str] = []
    
    base_prompt = """
    You are an expert data extraction agent. Your task is to extract line item details from the provided bill/invoice images.
    The input consists of one or more pages of a SINGLE document.

    GOAL: Extract all purchasable items from ALL pages such that the sum of their 'item_amount' equals the Final Bill Total.

    CRITICAL INSTRUCTIONS:
    1. **Distinguish Amounts vs. Identifiers**: Do NOT confuse IDs/Dates with monetary amounts.
    2. **No Subtotals**: Do NOT extract 'Sub-total', 'Total', 'Tax', or 'Discount' lines as items.
    3. **Charges are Items**: DO extract distinct charges like 'Pharmacy Charge', 'Shipping'.
    4. **Exact Values**: Extract 'item_amount' exactly as shown.
    5. **Page Mapping**: The input images correspond to pages 1, 2, 3... in order. Assign items to the correct 'page_no'.
    6. **Use OCR Context**: Raw OCR text is provided below to help you decipher blurry text or ambiguous numbers. Use it to validate your visual understanding.

    Output strictly in JSON format matching this structure:
    {
      "pagewise_line_items": [
        {
          "page_no": "1",
          "page_type": "Bill Detail",
          "bill_items": [
            { "item_name": "...", "item_amount": 0.0, "item_rate": 0.0, "item_quantity": 0.0 }
          ]
        }
      ],
      "total_item_count": 0
    }
    """
    
    if ocr_context:
        prompt = base_prompt + f"\n\n=== RAW OCR TEXT CONTEXT ===\n{ocr_context}"
    else:
        prompt = base_prompt

    content = []
    for mime_type, media_bytes in media_payloads:
        if mime_type.startswith("image/"):
            base64_image = base64.b64encode(media_bytes).decode("utf-8")
            content.append(
                {
                    "type": "image_url",
                    "image_url": f"data:{mime_type};base64,{base64_image}",
                }
            )

    content.append({"type": "text", "text": prompt})

    with tracing.span("gemini_call", run_type="chain", inputs={"pages": len(media_payloads)},
                      pages=len(media_payloads), has_ocr=bool(ocr_context)) as sp:
        return await _call_with_fallbacks(content, model_errors, sp)


async def _call_with_fallbacks(content: list, model_errors: list[str], sp: "tracing.Span") -> dict[str, Any]:
    max_retries = MAX_RETRIES
    attempts = 0
    quota_errors = 0
    for model_name in _candidate_models():
        llm = _get_llm(model_name)
        for attempt in range(max_retries):
            attempts += 1
            try:
                response = await llm.ainvoke([HumanMessage(content=content)])
                data_dict = response.model_dump()

                total_count = sum(
                    len(page.get("bill_items", []))
                    for page in data_dict.get("pagewise_line_items", [])
                )
                data_dict["total_item_count"] = total_count
                sp.set(model=model_name, attempts=attempts, quota_errors=quota_errors, item_count=total_count)
                return data_dict

            except Exception as e:
                error_str = str(e)
                is_quota_error = "429" in error_str or "Quota exceeded" in error_str or "ResourceExhausted" in error_str
                is_model_zero_quota = is_quota_error and f"model: {model_name}" in error_str and "limit: 0" in error_str
                quota_errors += int(is_quota_error)

                if is_model_zero_quota:
                    logger.warning("Model %s quota is currently zero. Trying next fallback model.", model_name)
                    model_errors.append(f"{model_name}: {error_str}")
                    break

                if is_quota_error and attempt < max_retries - 1:
                    wait_time = min(2 * (2 ** attempt), 10)
                    logger.warning(
                        "Quota exceeded for model %s (Attempt %d/%d). Retrying in %ds...",
                        model_name,
                        attempt + 1,
                        max_retries,
                        wait_time,
                    )
                    await asyncio.sleep(wait_time)
                    continue

                logger.error("Gemini API Error on model %s: %s", model_name, e)
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.5)
                    continue

                model_errors.append(f"{model_name}: {error_str}")

    joined_errors = " | ".join(model_errors) if model_errors else "No model candidates succeeded"
    sp.set(attempts=attempts, quota_errors=quota_errors, failed_models=_candidate_models())
    raise ValueError(f"Gemini API failed across candidate models after retries: {joined_errors}")
