"""LangSmith tracing (docs/context/architecture.md, Observability).

The only module that imports the LangSmith SDK. Tracing is off unless LANGSMITH_TRACING=true and
LANGSMITH_API_KEY is set. When it is on:
- `setup()` installs one global LangSmith client whose inputs and outputs pass through `scrub()`. The
  same client is used by LangChain/LangGraph's automatic traces (the Gemini call and the graph nodes) and
  by our own `span()`s, so nothing reaches LangSmith unscrubbed.
- `scrub()` keeps prompts, extracted rows, timings, tokens and errors. It drops raw file bytes and the
  raw OCR text. Page images sent to the model (base64 data URLs) are dropped too, unless
  LANGSMITH_INCLUDE_IMAGES=true (owner's decision, 2026-11-21: images opt-in; off by default). Traced
  images are a downscaled, low-quality copy so traces load quickly; the model gets the original.
- Tracing fails open: if LangSmith is down or misconfigured, requests run exactly as without it.
"""
from __future__ import annotations

import base64
import functools
import io
import logging
import os
from contextlib import contextmanager
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

OCR_MARKER = "=== RAW OCR TEXT CONTEXT ==="
DROP_KEYS = {"ocr_context", "file_data", "media_payload", "media_payloads"}
_MAX_DEPTH = 40

_enabled = False
_status = "off: setup() not called"  # why tracing is on or off, for the startup log


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes"}


def is_enabled() -> bool:
    return _enabled


def status() -> str:
    """One line saying whether tracing is on and, if not, why. Logged by the API at startup, because
    setup() runs at import time, before logging is configured, so its own log lines are lost."""
    return _status


def include_images() -> bool:
    """Read at call time so the flag can be flipped without re-importing (and in tests)."""
    return _truthy(os.getenv("LANGSMITH_INCLUDE_IMAGES"))


TRACE_IMAGE_MAX_SIDE = 1200  # px; enough to read a bill line, far smaller than the 200-DPI page
TRACE_IMAGE_QUALITY = 55  # JPEG quality for the traced copy only


@functools.lru_cache(maxsize=32)
def _shrink_data_url(data_url: str) -> str:
    """Downscale and recompress an image data URL for the trace. The model still gets the original.

    Cached because the same image appears in several runs of one trace (span, chain, chat model).
    """
    try:
        from PIL import Image

        raw = base64.b64decode(data_url.split(";base64,", 1)[1])
        with Image.open(io.BytesIO(raw)) as img:
            img = img.convert("L" if img.mode in ("L", "LA", "1") else "RGB")
            img.thumbnail((TRACE_IMAGE_MAX_SIDE, TRACE_IMAGE_MAX_SIDE))
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=TRACE_IMAGE_QUALITY, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")
    except Exception:  # never send an image we couldn't process
        return "<image omitted: could not shrink>"


def scrub(data: Any, _depth: int = 0) -> Any:
    """Return a copy of a trace payload without page images, file bytes or raw OCR text."""
    if _depth > _MAX_DEPTH:
        return "<omitted: too deep>"
    if isinstance(data, (bytes, bytearray, memoryview)):
        return f"<{len(data)} bytes omitted>"
    if isinstance(data, str):
        if data.startswith("data:") and ";base64," in data[:100]:
            return _shrink_data_url(data) if include_images() else "<image omitted>"
        if OCR_MARKER in data:
            return data[: data.index(OCR_MARKER)] + f"{OCR_MARKER}\n<OCR text omitted>"
        return data
    if isinstance(data, dict):
        out = {}
        for key, value in data.items():
            if key in DROP_KEYS:
                out[key] = "<omitted>"
            else:
                out[key] = scrub(value, _depth + 1)
        return out
    if isinstance(data, (list, tuple)):
        return [scrub(v, _depth + 1) for v in data]
    if hasattr(data, "getpixel") and hasattr(data, "size"):  # a PIL image
        return f"<image {getattr(data, 'size', '')} omitted>"
    if hasattr(data, "model_dump") and callable(data.model_dump):  # pydantic model, e.g. extracted rows
        try:
            return scrub(data.model_dump(), _depth + 1)
        except Exception:
            return f"<{type(data).__name__} omitted>"
    return data


def masked(data: dict | None) -> dict:
    try:
        return scrub(data or {})
    except Exception:  # never let masking errors leak data: drop the payload instead
        logger.exception("trace scrub failed; payload dropped")
        return {"note": "<payload dropped: scrub failed>"}


def setup(client: Any = None) -> bool:
    """Configure LangSmith once. Returns True when tracing is on. Safe to call more than once.

    `client` is for tests: a langsmith.Client built with `hide_inputs=masked, hide_outputs=masked`.
    """
    global _enabled, _status
    tracing_flag = _truthy(os.getenv("LANGSMITH_TRACING"))
    has_key = bool(os.getenv("LANGSMITH_API_KEY", "").strip())
    want = tracing_flag and has_key
    try:
        import langsmith as ls

        if not want:
            # Also stops LangChain's own auto-tracing if LANGSMITH_TRACING is set without a key.
            ls.configure(enabled=False)
            _enabled = False
            _status = ("off: LANGSMITH_TRACING=true but LANGSMITH_API_KEY is empty" if tracing_flag
                       else "off: LANGSMITH_TRACING is not true")
            return False
        # Where traces go is left to the SDK's own env vars: LANGSMITH_AGENT_ID/_ENVIRONMENT (an agent
        # address) or LANGSMITH_PROJECT. Passing a project here would silently override an agent address.
        ls.configure(
            client=client or ls.Client(hide_inputs=masked, hide_outputs=masked),
            enabled=True,
            metadata={"app_env": os.getenv("APP_ENV", "local")},
        )
        _enabled = True
        where = os.getenv("LANGSMITH_AGENT_ID") and (
            f"agent {os.getenv('LANGSMITH_AGENT_ID')} / {os.getenv('LANGSMITH_AGENT_ENVIRONMENT', '?')}")
        _status = f"on ({where or 'project ' + os.getenv('LANGSMITH_PROJECT', 'default')})"
        logger.info("LangSmith tracing %s", _status)
    except Exception:
        logger.exception("LangSmith setup failed; tracing off")
        _enabled = False
        _status = "off: LangSmith setup failed (see the error above)"
    return _enabled


def traceable(name: str, run_type: str = "chain", inputs: Callable[[dict], dict] | None = None,
              outputs: Callable[[Any], dict] | None = None) -> Callable:
    """Decorator for plain functions (sync or async), per the langsmith-trace skill's guidance for code that
    LangChain doesn't trace. A no-op at call time while tracing is off.

    `inputs`/`outputs` summarise arguments and return values (e.g. a page count instead of the pages). They
    run before `scrub()`, and if they fail LangSmith drops the payload instead of sending it raw.
    Caveat: work handed to `run_in_executor` loses the trace context; decorate the async caller instead.
    """
    try:
        from langsmith import traceable as _ls_traceable

        return _ls_traceable(name=name, run_type=run_type, process_inputs=inputs, process_outputs=outputs)
    except Exception:  # SDK missing or broken: leave the function untouched
        logger.debug("traceable unavailable; %s runs untraced", name, exc_info=True)
        return lambda fn: fn


class Span:
    """Handle yielded by `span()`. Methods are no-ops when tracing is off and never raise."""

    def __init__(self, run: Any = None):
        self._run = run

    def set(self, **metadata: Any) -> None:
        if self._run is None:
            return
        try:
            self._run.metadata.update(metadata)
        except Exception:
            logger.debug("span.set failed", exc_info=True)

    def output(self, outputs: dict) -> None:
        if self._run is None:
            return
        try:
            self._run.add_outputs(outputs)
        except Exception:
            logger.debug("span.output failed", exc_info=True)


@contextmanager
def span(name: str, run_type: str = "chain", inputs: dict | None = None, **metadata: Any) -> Iterator[Span]:
    """Trace a block as one LangSmith run, nested under any enclosing span or LangChain run.

    Exceptions from the block propagate unchanged (and are recorded on the run). Failures inside
    LangSmith itself are swallowed, so the block runs the same with tracing on or off.
    """
    if not _enabled:
        yield Span()
        return
    try:
        import langsmith as ls

        cm = ls.trace(name, run_type=run_type, inputs=inputs or {}, metadata=metadata)
        run = cm.__enter__()
    except Exception:
        logger.debug("span start failed; running untraced", exc_info=True)
        yield Span()
        return

    try:
        yield Span(run)
    except BaseException as exc:
        try:
            cm.__exit__(type(exc), exc, exc.__traceback__)
        except Exception:
            logger.debug("span close failed", exc_info=True)
        raise
    else:
        try:
            cm.__exit__(None, None, None)
        except Exception:
            logger.debug("span close failed", exc_info=True)


def flush() -> None:
    """Send queued traces before the process exits (API shutdown, end of an eval run)."""
    if not _enabled:
        return
    try:
        from langsmith import run_trees

        if run_trees._CLIENT is not None:  # noqa: SLF001
            run_trees._CLIENT.flush()  # noqa: SLF001
    except Exception:
        logger.debug("trace flush failed", exc_info=True)
