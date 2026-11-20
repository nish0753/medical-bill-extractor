"""LangSmith tracing (docs/context/architecture.md, Observability).

The only module that imports the LangSmith SDK. Tracing is off unless LANGSMITH_TRACING=true and
LANGSMITH_API_KEY is set. When it is on:
- `setup()` installs one global LangSmith client whose inputs and outputs pass through `scrub()`. The
  same client is used by LangChain/LangGraph's automatic traces (the Gemini call and the graph nodes) and
  by our own `span()`s, so nothing reaches LangSmith unscrubbed.
- `scrub()` keeps prompts, extracted rows, timings, tokens and errors. It drops page images, raw file
  bytes and the raw OCR text (owner's decision, 2026-11-20: "extracted rows, no page images").
- Tracing fails open: if LangSmith is down or misconfigured, requests run exactly as without it.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

OCR_MARKER = "=== RAW OCR TEXT CONTEXT ==="
DROP_KEYS = {"ocr_context", "file_data", "media_payload", "media_payloads"}
_MAX_DEPTH = 40

_enabled = False


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes"}


def is_enabled() -> bool:
    return _enabled


def scrub(data: Any, _depth: int = 0) -> Any:
    """Return a copy of a trace payload without page images, file bytes or raw OCR text."""
    if _depth > _MAX_DEPTH:
        return "<omitted: too deep>"
    if isinstance(data, (bytes, bytearray, memoryview)):
        return f"<{len(data)} bytes omitted>"
    if isinstance(data, str):
        if data.startswith("data:") and ";base64," in data[:100]:
            return "<image omitted>"
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
    global _enabled
    want = _truthy(os.getenv("LANGSMITH_TRACING")) and bool(os.getenv("LANGSMITH_API_KEY", "").strip())
    try:
        import langsmith as ls

        if not want:
            # Also stops LangChain's own auto-tracing if LANGSMITH_TRACING is set without a key.
            ls.configure(enabled=False)
            _enabled = False
            return False
        # Where traces go is left to the SDK's own env vars: LANGSMITH_AGENT_ID/_ENVIRONMENT (an agent
        # address) or LANGSMITH_PROJECT. Passing a project here would silently override an agent address.
        ls.configure(
            client=client or ls.Client(hide_inputs=masked, hide_outputs=masked),
            enabled=True,
            metadata={"app_env": os.getenv("APP_ENV", "local")},
        )
        _enabled = True
        logger.info("LangSmith tracing on")
    except Exception:
        logger.exception("LangSmith setup failed; tracing off")
        _enabled = False
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
