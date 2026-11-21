"""Offline tests for app/tracing.py: masking, fail-open behaviour, and what would actually be sent to LangSmith.

No network: the LangSmith client points at a closed local port, and the test client records the payloads
after masking (the exact data LangSmith would receive).
"""
import asyncio
import json

import pytest
from PIL import Image

from app import extractor, tracing  # import extractor first: its import runs tracing.setup()

PIXELS = "QUJDREVGR0g" * 20  # stands in for base64 page pixels
OCR_SECRET = "PATIENT-NAME-FROM-OCR"


class TestScrub:
    def test_drops_images_bytes_and_ocr_but_keeps_rows(self):
        data = {
            "file_data": b"%PDF-1.7 ...",
            "pages": [{"page_no": 1, "media_payload": ("image/jpeg", b"\xff\xd8"), "ocr_context": OCR_SECRET}],
            "messages": [[{"content": [
                {"type": "image_url", "image_url": f"data:image/jpeg;base64,{PIXELS}"},
                {"type": "text", "text": f"Extract rows.\n=== RAW OCR TEXT CONTEXT ===\n{OCR_SECRET}"},
            ]}]],
            "raw": b"\x00\x01",
            "result": {"bill_items": [{"item_name": "CBC", "item_amount": 240.0}]},
        }
        text = json.dumps(tracing.scrub(data))
        assert PIXELS not in text and OCR_SECRET not in text and "%PDF" not in text
        assert "Extract rows." in text and "CBC" in text and "240.0" in text
        assert "<image omitted>" in text and "<OCR text omitted>" in text

    def test_include_images_keeps_image_but_still_drops_ocr_and_bytes(self, monkeypatch):
        monkeypatch.setenv("LANGSMITH_INCLUDE_IMAGES", "true")
        data = {
            "file_data": b"%PDF-1.7 ...",
            "pages": [{"page_no": 1, "media_payload": ("image/jpeg", b"\xff\xd8"), "ocr_context": OCR_SECRET}],
            "messages": [[{"content": [
                {"type": "image_url", "image_url": f"data:image/jpeg;base64,{PIXELS}"},
                {"type": "text", "text": f"Extract rows.\n=== RAW OCR TEXT CONTEXT ===\n{OCR_SECRET}"},
            ]}]],
        }
        text = json.dumps(tracing.scrub(data))
        assert PIXELS not in text  # not a real image: dropped, never sent as-is
        assert "<image omitted: could not shrink>" in text
        assert OCR_SECRET not in text and "%PDF" not in text

    def test_included_images_are_shrunk(self, monkeypatch):
        import base64
        import io
        import random

        monkeypatch.setenv("LANGSMITH_INCLUDE_IMAGES", "true")
        rng = random.Random(0)
        page = Image.new("RGB", (1700, 2200), "white")  # A4 at 200 DPI
        page.putdata([(v, v, v) for v in (rng.randrange(256) for _ in range(1700 * 2200))])
        buf = io.BytesIO()
        page.save(buf, format="JPEG", quality=95)
        original = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

        traced = tracing.scrub({"image_url": original})["image_url"]
        assert traced.startswith("data:image/jpeg;base64,")
        assert len(traced) < len(original) / 3
        with Image.open(io.BytesIO(base64.b64decode(traced.split(",", 1)[1]))) as img:
            assert max(img.size) == tracing.TRACE_IMAGE_MAX_SIDE

    def test_masked_never_raises_and_drops_payload_on_failure(self, monkeypatch):
        monkeypatch.setattr(tracing, "scrub", lambda _d: 1 / 0)
        assert tracing.masked({"x": "y"}) == {"note": "<payload dropped: scrub failed>"}


class TestSpanWhenOff:
    def test_off_without_key(self, monkeypatch):
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
        assert tracing.setup() is False
        assert tracing.status() == "off: LANGSMITH_TRACING=true but LANGSMITH_API_KEY is empty"

    def test_status_says_why_when_flag_off(self, monkeypatch):
        monkeypatch.setenv("LANGSMITH_TRACING", "false")
        monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
        assert tracing.setup() is False
        assert tracing.status() == "off: LANGSMITH_TRACING is not true"

    def test_noop_span_runs_block_and_propagates_errors(self):
        tracing._enabled = False
        with tracing.span("x") as sp:
            sp.set(a=1)
            sp.output({"b": 2})
        with pytest.raises(ValueError):
            with tracing.span("x"):
                raise ValueError("boom")


@pytest.fixture
def recording_client(monkeypatch):
    """Tracing on, pointed at a closed port; records masked inputs/outputs as LangSmith would send them."""
    import langsmith as ls

    sent: list = []

    class RecordingClient(ls.Client):
        def _hide_run_inputs(self, inputs):
            out = super()._hide_run_inputs(inputs)
            sent.append(out)
            return out

        def _hide_run_outputs(self, outputs):
            out = super()._hide_run_outputs(outputs)
            sent.append(out)
            return out

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "http://127.0.0.1:9")
    client = RecordingClient(api_key="test-key", api_url="http://127.0.0.1:9",
                             hide_inputs=tracing.masked, hide_outputs=tracing.masked)
    assert tracing.setup(client=client) is True
    yield sent
    tracing.flush()
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    tracing.setup()


def _wait_for_tracers():
    from langchain_core.tracers.langchain import wait_for_all_tracers

    wait_for_all_tracers()


class TestWhatIsSent:
    def test_chat_model_run_has_no_image_or_ocr(self, recording_client):
        """The real risk path: LangChain auto-traces the model call, including the image message."""
        from langchain_core.language_models.fake_chat_models import FakeListChatModel
        from langchain_core.messages import HumanMessage

        llm = FakeListChatModel(responses=['{"bill_items": [{"item_name": "CBC", "item_amount": 240}]}'])
        msg = HumanMessage(content=[
            {"type": "image_url", "image_url": f"data:image/jpeg;base64,{PIXELS}"},
            {"type": "text", "text": f"Extract rows.\n=== RAW OCR TEXT CONTEXT ===\n{OCR_SECRET}"},
        ])
        with tracing.span("extract_bill"):
            llm.invoke([msg])
        _wait_for_tracers()
        text = json.dumps(recording_client, default=str)
        assert recording_client, "nothing was traced"
        assert PIXELS not in text and OCR_SECRET not in text
        assert "Extract rows." in text and "CBC" in text

    def test_pipeline_traces_without_bytes_and_still_works_offline(self, recording_client, monkeypatch):
        """Whole pipeline with tracing on and LangSmith unreachable: same result, nothing sensitive sent."""
        monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
        class FakeTesseract:  # OCR "reads" a patient name; it must never reach LangSmith
            @staticmethod
            def image_to_string(_image):
                return f"{OCR_SECRET}\nROOM RENT 1000"

        monkeypatch.setattr(extractor, "pytesseract", FakeTesseract)
        monkeypatch.setattr(extractor, "convert_from_bytes",
                            lambda _d, dpi: [Image.new("RGB", (40, 40), "white") for _ in range(3)])

        async def fake_llm(media_payloads, ocr_context=""):
            return {"pagewise_line_items": [{"page_no": "1", "page_type": "Bill Detail", "bill_items": [
                {"item_name": "ROOM RENT", "item_amount": 1000, "item_rate": 1000, "item_quantity": 1}]}],
                "total_item_count": 1}

        monkeypatch.setattr(extractor, "call_gemini_api_async", fake_llm)
        result = asyncio.run(extractor.extract_bill_data(b"%PDF-1.7 fake", mime_type="application/pdf"))
        assert result["total_item_count"] == 2  # 3 pages in 2 batches, one row per batch
        _wait_for_tracers()
        text = json.dumps(recording_client, default=str)
        assert "ROOM RENT" in text
        assert OCR_SECRET not in text
        assert "%PDF" not in text and "\\xff\\xd8" not in text and "base64" not in text
        # The plain-Python steps are traced, as summaries only.
        assert '"page_count": 3' in text and '"chars": ' in text and '"image_size": [40, 40]' in text
