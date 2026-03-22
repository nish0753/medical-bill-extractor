"""Unit tests for core utilities and logic."""
import pytest


# ---- Tests for safe_float ----
from app.main import safe_float


class TestSafeFloat:
    def test_normal_float(self):
        assert safe_float(3.14) == 3.14

    def test_string_with_commas(self):
        assert safe_float("1,234.56") == 1234.56

    def test_none_returns_zero(self):
        assert safe_float(None) == 0.0

    def test_invalid_string_returns_zero(self):
        assert safe_float("abc") == 0.0

    def test_integer(self):
        assert safe_float(42) == 42.0

    def test_empty_string(self):
        assert safe_float("") == 0.0


# ---- Tests for build_response ----
from app.main import build_response


class TestBuildResponse:
    def test_valid_input(self):
        result = {
            "pagewise_line_items": [
                {
                    "page_no": "1",
                    "page_type": "Bill Detail",
                    "bill_items": [
                        {"item_name": "Aspirin", "item_amount": 10.0, "item_rate": 5.0, "item_quantity": 2.0}
                    ],
                }
            ],
            "total_item_count": 1,
        }
        resp = build_response(result)
        assert resp["is_success"] is True
        assert resp["data"]["total_item_count"] == 1
        assert resp["data"]["pagewise_line_items"][0]["bill_items"][0]["item_name"] == "Aspirin"

    def test_empty_input(self):
        resp = build_response({})
        assert resp["is_success"] is True
        assert resp["data"]["total_item_count"] == 0

    def test_non_dict_input(self):
        resp = build_response("garbage")
        assert resp["is_success"] is True
        assert resp["data"]["total_item_count"] == 0

    def test_malformed_items_skipped(self):
        result = {
            "pagewise_line_items": [
                {
                    "page_no": "1",
                    "page_type": "Bill Detail",
                    "bill_items": ["not_a_dict", {"item_name": "Valid", "item_amount": 5}],
                }
            ]
        }
        resp = build_response(result)
        assert resp["data"]["total_item_count"] == 1


# ---- Tests for preprocess_image ----
from app.extractor import preprocess_image
from PIL import Image
import io
import asyncio
import app.extractor as extractor


class TestPreprocessImage:
    def _make_test_image(self) -> bytes:
        img = Image.new("RGB", (100, 100), color="red")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        return buf.getvalue()

    def test_returns_bytes(self):
        result = preprocess_image(self._make_test_image())
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_invalid_bytes_returns_original(self):
        garbage = b"not_an_image"
        result = preprocess_image(garbage)
        assert result == garbage


class TestLangGraphBatchAggregation:
    def test_extract_bill_data_batches_and_aggregates_pages(self, monkeypatch):
        # Keep this test fully offline by stubbing OCR, PDF conversion, and LLM extraction.
        sample_pdf_bytes = b"%PDF-1.7 mocked-content"

        def fake_convert_from_bytes(_file_data, dpi):
            assert dpi > 0
            return [
                Image.new("RGB", (50, 50), color="white"),
                Image.new("RGB", (50, 50), color="white"),
                Image.new("RGB", (50, 50), color="white"),
            ]

        calls = []

        async def fake_call_gemini_api_async(media_payloads, ocr_context=""):
            calls.append((len(media_payloads), bool(ocr_context)))
            if len(calls) == 1:
                return {
                    "pagewise_line_items": [
                        {
                            "page_no": "1",
                            "page_type": "Bill Detail",
                            "bill_items": [{"item_name": "A", "item_amount": 10, "item_rate": 10, "item_quantity": 1}],
                        },
                        {
                            "page_no": "2",
                            "page_type": "Bill Detail",
                            "bill_items": [{"item_name": "B", "item_amount": 20, "item_rate": 20, "item_quantity": 1}],
                        },
                    ],
                    "total_item_count": 2,
                }

            return {
                "pagewise_line_items": [
                    {
                        "page_no": "3",
                        "page_type": "Bill Detail",
                        "bill_items": [{"item_name": "C", "item_amount": 30, "item_rate": 30, "item_quantity": 1}],
                    }
                ],
                "total_item_count": 1,
            }

        monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
        monkeypatch.setattr(extractor, "pytesseract", None)
        monkeypatch.setattr(extractor, "BATCH_SIZE", 2)
        monkeypatch.setattr(extractor, "convert_from_bytes", fake_convert_from_bytes)
        monkeypatch.setattr(extractor, "call_gemini_api_async", fake_call_gemini_api_async)

        # Ensure graph recompiles with patched settings in this test scope.
        monkeypatch.setattr(extractor, "_workflow", None)

        result = asyncio.run(extractor.extract_bill_data(sample_pdf_bytes, mime_type="application/octet-stream"))

        assert len(calls) == 2
        assert calls[0][0] == 2
        assert calls[1][0] == 1
        assert len(result["pagewise_line_items"]) == 3
        assert result["total_item_count"] == 3


class TestGeminiRetryBehavior:
    def test_call_gemini_retries_on_quota_then_succeeds(self, monkeypatch):
        sleep_calls = []

        class FakeResponse:
            def model_dump(self):
                return {
                    "pagewise_line_items": [
                        {
                            "page_no": "1",
                            "page_type": "Bill Detail",
                            "bill_items": [
                                {
                                    "item_name": "Retry Item",
                                    "item_amount": 42.0,
                                    "item_rate": 42.0,
                                    "item_quantity": 1.0,
                                }
                            ],
                        }
                    ],
                    "total_item_count": 0,
                }

        class FakeLLM:
            def __init__(self):
                self.calls = 0

            async def ainvoke(self, _messages):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("429 Quota exceeded")
                return FakeResponse()

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        fake_llm = FakeLLM()

        monkeypatch.setattr(extractor, "_get_llm", lambda _model_name: fake_llm)
        monkeypatch.setattr(extractor, "MAX_RETRIES", 3)
        monkeypatch.setattr(extractor, "USE_MOCK_MODE", False)
        monkeypatch.setattr(extractor.asyncio, "sleep", fake_sleep)

        result = asyncio.run(
            extractor.call_gemini_api_async(
                [("image/jpeg", b"fake-image")],
                "",
            )
        )

        assert fake_llm.calls == 2
        assert sleep_calls == [2]
        assert result["total_item_count"] == 1
        assert result["pagewise_line_items"][0]["bill_items"][0]["item_name"] == "Retry Item"


# ---- Tests for FastAPI endpoints ----
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


class TestEndpoints:
    def test_root_returns_html(self):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_health_check(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"

    def test_extract_bill_invalid_url_scheme(self):
        resp = client.post("/extract-bill-data", json={"document": "ftp://evil.com/file.pdf"})
        assert resp.status_code == 422  # Validation error

    def test_extract_bill_missing_document(self):
        resp = client.post("/extract-bill-data", json={})
        assert resp.status_code == 422

    def test_extract_bill_empty_url(self):
        resp = client.post("/extract-bill-data", json={"document": ""})
        assert resp.status_code == 422
