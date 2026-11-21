"""Alternative model backends for the old pipeline, used only by the evaluation runner.

`use_openai_compatible(...)` swaps app.extractor.call_gemini_api_async for a function that sends the SAME
prompt, page images, OCR context and output schema to any OpenAI-compatible chat API: a self-hosted model on
Modal (vLLM/SGLang) or OpenAI itself. Everything else in the old pipeline (page rendering, Tesseract,
2-page batches, response building) stays identical, so a score difference is a model difference.
App code is not modified; the swap happens in the runner process only.
"""
from __future__ import annotations

import ast
import asyncio
import base64
import inspect
import time
from typing import Any

import httpx

USAGE: list[dict] = []  # one entry per model call: tokens and seconds, for cost reporting


def _legacy_prompt() -> str:
    """Read the old pipeline's base prompt from its source, so both backends get identical text."""
    from app import extractor

    tree = ast.parse(inspect.getsource(extractor.call_gemini_api_async))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "base_prompt":
            return node.value.value
    raise RuntimeError("base_prompt not found in app.extractor.call_gemini_api_async")


def _as_rgb(data: bytes) -> bytes:
    """SGLang's Qwen image processor crashes on 1-channel (grayscale) images, which the old pipeline
    produces for single-image uploads. Re-save as 3-channel RGB: identical pixels, so the model sees the
    same picture Gemini sees."""
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(data))
    if img.mode == "RGB":
        return data
    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=95)
    return out.getvalue()


def use_openai_compatible(base_url: str, model: str, headers: dict[str, str], extra_body: dict | None = None,
                          timeout: float = 600.0) -> None:
    from app import extractor

    base_prompt = _legacy_prompt()
    schema = extractor.LLMExtractionOutput.model_json_schema()
    # follow_redirects: Modal answers requests that run past ~150 s (e.g. during a cold start) with a 303 to
    # a URL that waits for the result. Without following it the call fails although the model is fine.
    client = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout,
                               follow_redirects=True)

    async def call(media_payloads: list[tuple[str, bytes]], ocr_context: str = "") -> dict[str, Any]:
        prompt = base_prompt + (f"\n\n=== RAW OCR TEXT CONTEXT ===\n{ocr_context}" if ocr_context else "")
        content: list[dict] = [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(_as_rgb(data)).decode()}"}}
            for mime, data in media_payloads if mime.startswith("image/")
        ]
        content.append({"type": "text", "text": prompt})
        body = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "LLMExtractionOutput", "schema": schema}},
            "temperature": 0,
            **(extra_body or {}),
        }
        last_error = None
        for attempt in range(3):  # transient errors only (cold start, 5xx); never on a parsed bad answer
            start = time.monotonic()
            try:
                resp = await client.post("/chat/completions", json=body)
                if resp.status_code >= 500 or resp.status_code == 429:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    await asyncio.sleep(10 * (attempt + 1))
                    continue
                resp.raise_for_status()
                data = resp.json()
                break
            except httpx.TimeoutException as exc:
                last_error = f"timeout: {exc}"
        else:
            raise RuntimeError(f"model call failed after retries: {last_error}")

        usage = data.get("usage") or {}
        USAGE.append({"seconds": round(time.monotonic() - start, 1), "prompt_tokens": usage.get("prompt_tokens"),
                      "completion_tokens": usage.get("completion_tokens"), "pages": len(media_payloads)})
        text = data["choices"][0]["message"].get("content") or ""
        parsed = extractor.LLMExtractionOutput.model_validate_json(text).model_dump()
        parsed["total_item_count"] = sum(len(p.get("bill_items", [])) for p in parsed.get("pagewise_line_items", []))
        return parsed

    extractor.call_gemini_api_async = call
