"""Run the current (pre-redesign) pipeline on every training sample and save its API responses (M0 baseline).

    python -m eval.run_baseline --out eval/outputs/baseline-legacy [--only 3 7] [--force] [--pause 20]
    python -m eval.run_baseline --out eval/outputs/cord-legacy --inputs ../EXTERNAL_DATASETS/cord/images
    python -m eval.run_baseline --out eval/outputs/qwen36-legacy --backend qwen-modal --budget-usd 15 --gpu-rate 6.25

Backends: `gemini` (the app as it is) or `qwen-modal` (same pipeline, model call sent to a Modal endpoint;
env TOKENID / TOKEN_SECRET (Modal proxy-auth token); optional QWEN_BASE_URL, QWEN_MODEL). See eval/backends.py.
--budget-usd stops the run once (elapsed time x --gpu-rate) passes the budget: a dedicated GPU endpoint
bills for time while it is up, not per request.

Calls app.extractor.extract_bill_data and app.main.build_response, i.e. exactly what
POST /extract-from-file returns, without starting the server. Needs GOOGLE_API_KEY (env or .env).
Existing outputs are skipped unless --force, so an interrupted run (quota) can be resumed.
Both splits are run; run_eval keeps the test split to aggregates.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PDFS = REPO.parent / "TRAINING_SAMPLES"


MIME = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


async def run_one(path: Path) -> dict:
    from app.extractor import extract_bill_data
    from app.main import build_response

    try:
        result = await extract_bill_data(path.read_bytes(), MIME[path.suffix.lower()])
        return build_response(result)
    except Exception as exc:  # the API turns any failure into is_success: false with HTTP 200
        return {"is_success": False, "error": f"{type(exc).__name__}: {exc}"[:500], "data": None}


async def run_all(files: list[Path], out: Path, force: bool, pause: float,
                  budget_usd: float | None = None, gpu_rate: float = 0.0) -> None:
    from . import backends

    t0 = time.monotonic()
    meta_path = out / "_run_meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    todo = [f for f in files if force or not (out / f"{f.stem}.json").exists()]
    print(f"{len(files) - len(todo)} already done, {len(todo)} to run", flush=True)
    for i, path in enumerate(todo):
        spent = (time.monotonic() - t0) / 3600 * gpu_rate
        if budget_usd is not None and spent >= budget_usd:
            print(f"BUDGET REACHED: ~${spent:.2f} of ${budget_usd:.2f}; stopping before {path.stem}", flush=True)
            break
        start = time.monotonic()
        n_calls = len(backends.USAGE)
        resp = await run_one(path)
        secs = round(time.monotonic() - start, 1)
        (out / f"{path.stem}.json").write_text(json.dumps(resp, indent=2) + "\n")
        items = (resp.get("data") or {}).get("total_item_count")
        calls = backends.USAGE[n_calls:]
        meta[path.stem] = {"seconds": secs, "is_success": resp.get("is_success"), "items": items, "error": resp.get("error"),
                           "model_calls": len(calls),
                           "prompt_tokens": sum(c["prompt_tokens"] or 0 for c in calls) or None,
                           "completion_tokens": sum(c["completion_tokens"] or 0 for c in calls) or None}
        meta_path.write_text(json.dumps(meta, indent=2) + "\n")
        print(f"{path.stem}: {'ok' if resp.get('is_success') else 'FAILED'} in {secs}s, {items} items"
              + (f" ({resp.get('error')})" if not resp.get("is_success") else ""), flush=True)
        if i < len(todo) - 1:
            await asyncio.sleep(pause)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--only", type=int, nargs="*", help="sample numbers (default: all 15)")
    ap.add_argument("--inputs", type=Path, help="folder of PDFs/images to run instead of the 15 training samples")
    ap.add_argument("--force", action="store_true", help="re-run samples that already have an output")
    ap.add_argument("--pause", type=float, default=20.0, help="seconds between documents (free-tier rate limits)")
    ap.add_argument("--backend", choices=("gemini", "qwen-modal"), default="gemini")
    ap.add_argument("--budget-usd", type=float, help="stop when elapsed time x --gpu-rate reaches this")
    ap.add_argument("--gpu-rate", type=float, default=0.0, help="USD per hour of the endpoint's GPU")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(REPO))
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env")
    if not os.getenv("GOOGLE_API_KEY"):
        print("GOOGLE_API_KEY is not set (environment or .env). Nothing run.")
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    if args.inputs:
        files = sorted(f for f in args.inputs.iterdir() if f.suffix.lower() in MIME)
    else:
        files = [PDFS / f"train_sample_{n}.pdf" for n in (args.only or range(1, 16))]
    # One event loop for the whole run, as in the API server: the old pipeline caches its Gemini client,
    # which is bound to the loop it was created on. A loop per document breaks every call after the first.
    if args.backend == "qwen-modal":
        from .backends import use_openai_compatible

        key = os.getenv("MODAL_PROXY_KEY") or os.getenv("TOKENID")
        secret = os.getenv("MODAL_PROXY_SECRET") or os.getenv("TOKEN_SECRET")
        if not (key and secret):
            print("Missing Modal proxy-auth token in .env (TOKENID / TOKEN_SECRET)")
            return 1
        use_openai_compatible(
            base_url=os.getenv("QWEN_BASE_URL", "https://nishant0753--ep-medical-bill-server.us-west.modal.direct/v1"),
            model=os.getenv("QWEN_MODEL", "medical-bill"),
            headers={"Modal-Key": key.strip(), "Modal-Secret": secret.strip()},
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
    try:
        asyncio.run(run_all(files, args.out, args.force, args.pause, args.budget_usd, args.gpu_rate))
    finally:
        from app import tracing

        tracing.flush()  # eval runs are traced too when LANGSMITH_TRACING=true
    return 0


if __name__ == "__main__":
    sys.exit(main())
