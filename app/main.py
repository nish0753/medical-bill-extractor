from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
from starlette.requests import Request
from .extractor import extract_bill_data
from .schemas import FinalResponse
import logging
import requests
import asyncio
from urllib.parse import urlparse

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Configuration ---
MAX_UPLOAD_SIZE_MB = 20
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024
ALLOWED_URL_SCHEMES = {"http", "https"}
ZERO_TOKEN_USAGE = {"total_tokens": 0, "input_tokens": 0, "output_tokens": 0}
EMPTY_DATA_BLOCK = {"pagewise_line_items": [], "total_item_count": 0}

app = FastAPI(
    title="Automated Bill Extraction System",
    description="A Hybrid AI Pipeline for extracting structured data from medical bills.",
    version="1.1.0",
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _base_response(is_success: bool, data: dict | None = None, error: str | None = None) -> dict:
    response = {
        "is_success": is_success,
        "token_usage": ZERO_TOKEN_USAGE.copy(),
        "data": data if data is not None else EMPTY_DATA_BLOCK.copy(),
    }
    if error:
        response["error"] = error
    return response

@app.exception_handler(Exception)
async def universal_exception_handler(request: Request, exc: Exception):
    logger.error("Error while extracting: %s", exc)
    return JSONResponse(status_code=500, content=_base_response(False, error="Internal server error"))

class BillRequest(BaseModel):
    document: str

    @field_validator("document")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        parsed = urlparse(v)
        if parsed.scheme not in ALLOWED_URL_SCHEMES:
            raise ValueError(f"URL scheme must be one of {ALLOWED_URL_SCHEMES}")
        if not parsed.netloc:
            raise ValueError("Invalid URL: missing host")
        return v


def _failure_response(error_message: str = "Extraction failed") -> dict:
    """Standard failure response to avoid duplication."""
    return _base_response(False, error=error_message)


def safe_float(value) -> float:
    """Converts anything to float safely."""
    try:
        if value is None:
            return 0.0
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


def _normalize_item(item: dict) -> dict:
    return {
        "item_name": item.get("item_name", "") or "",
        "item_amount": safe_float(item.get("item_amount")),
        "item_rate": safe_float(item.get("item_rate")),
        "item_quantity": safe_float(item.get("item_quantity")),
    }


def _normalize_page(page: dict) -> dict:
    items = page.get("bill_items", [])
    if not isinstance(items, list):
        items = []

    safe_items = [_normalize_item(item) for item in items if isinstance(item, dict)]
    return {
        "page_no": str(page.get("page_no", "1")),
        "page_type": page.get("page_type", "Bill Detail"),
        "bill_items": safe_items,
    }


def build_response(result: dict | None):
    """Fully safe wrapper for hackathon API."""
    if not isinstance(result, dict):
        result = {}

    pages = result.get("pagewise_line_items", [])
    if not isinstance(pages, list):
        pages = []

    page_entries = [_normalize_page(page) for page in pages if isinstance(page, dict)]

    total_items = sum(len(p["bill_items"]) for p in page_entries)
    data_block = {"pagewise_line_items": page_entries, "total_item_count": total_items}
    return _base_response(True, data=data_block)

def download_file_sync(url: str):
    """Synchronous download function to be run in executor."""
    response = requests.get(url, timeout=30, stream=True)
    response.raise_for_status()

    # Enforce download size limit
    content_length = response.headers.get("Content-Length")
    if content_length and int(content_length) > MAX_UPLOAD_SIZE_BYTES:
        raise ValueError(f"File too large (>{MAX_UPLOAD_SIZE_MB}MB)")

    content = response.content
    if len(content) > MAX_UPLOAD_SIZE_BYTES:
        raise ValueError(f"File too large (>{MAX_UPLOAD_SIZE_MB}MB)")

    return content, response.headers.get('Content-Type', '')

@app.post("/extract-bill-data", response_model=FinalResponse)
async def extract_bill(request: BillRequest):
    try:
        # Step 1: Download the document
        logger.info("Download starting...")
        logger.info("Received request for document: %s", request.document)
        
        # Run synchronous download in a separate thread to avoid blocking the event loop
        loop = asyncio.get_running_loop()
        pdf_bytes, content_type = await loop.run_in_executor(None, download_file_sync, request.document)
        
        logger.info("Download complete. Size: %d bytes. Type: %s", len(pdf_bytes), content_type)

        # Step 2: Process it
        logger.info("Processing Start...")
        # Pass bytes directly to extractor
        result = await extract_bill_data(pdf_bytes, mime_type=content_type)

        # Step 3: Wrap output into required format
        logger.info("Processing complete. Building response.")
        
        # Check for empty items
        if not result.get("pagewise_line_items") and result.get("total_item_count", 0) == 0:
             logger.warning("No items detected")

        response = build_response(result)
        item_count = response["data"]["total_item_count"]
        logger.info("SUCCESS: Extracted %d items from document: %s", item_count, request.document)
        return response

    except Exception as e:
        logger.error("FAILURE: Failed to extract from document: %s. Error: %s", request.document, e)
        return _failure_response(str(e))


@app.post("/extract-from-file", response_model=FinalResponse)
async def extract_from_file(file: UploadFile = File(...)):
    try:
        logger.info("Received file upload: %s", file.filename)
        contents = await file.read()

        if len(contents) > MAX_UPLOAD_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Maximum size is {MAX_UPLOAD_SIZE_MB}MB.",
            )

        result = await extract_bill_data(contents, mime_type=file.content_type)
        response = build_response(result)
        item_count = response["data"]["total_item_count"]
        logger.info("SUCCESS: Extracted %d items from file: %s", item_count, file.filename)
        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error("FAILURE: Failed to extract from file: %s. Error: %s", file.filename, e)
        return _failure_response(str(e))


@app.get("/")
async def read_index():
    return RedirectResponse(url="/docs")


@app.get("/health")
async def health_check():
    """Health check endpoint for monitoring and load balancers."""
    return {"status": "healthy", "version": "1.1.0"}
