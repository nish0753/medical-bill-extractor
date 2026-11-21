"""Self-hosted comparison model on Modal: vLLM's OpenAI-compatible server (docs/context/architecture.md,
Self-hosted Comparison Endpoint). Offline comparisons only; the live app never calls this.

    SERVE_GPU=H100 modal deploy serving/modal_vllm.py        # permanent URL
    SERVE_GPU=H100 modal serve  serving/modal_vllm.py        # temporary dev URL, stops with Ctrl+C
    SERVE_GPU=H100 modal run serving/modal_vllm.py::download # pre-fetch weights into the volume (runs on CPU)

Settings come from environment variables at deploy time (see SETTINGS below) and are baked into the
container image, so the deploy side and the container side always agree. Callers authenticate with a
Modal proxy-auth token (headers Modal-Key / Modal-Secret).

This folder is deliberately not called `modal/`: that would shadow the `modal` package.
"""
from __future__ import annotations

import os
import subprocess

import modal

VLLM_VERSION = "0.33.0"
PORT = 8000
MINUTES = 60

# name -> default. None means required at deploy time.
SETTINGS: dict[str, str | None] = {
    "SERVE_GPU": "H100",  # owner, 2026-11-21 (A100-80GB was too slow); e.g. "L40S", "A100-80GB", "H100:2"
    "SERVE_MODEL_ID": "Qwen/Qwen3.6-35B-A3B",
    "SERVE_MODEL_NAME": "medical-bill",
    "SERVE_MAX_MODEL_LEN": "32768",
    "SERVE_MAX_CONTAINERS": "1",
    "SERVE_SCALEDOWN_SECONDS": "300",  # shut down 5 min after the last request; next request cold-starts
    # 1 = skip torch.compile / CUDA-graph capture: ~1 min faster start, but measured 18 tok/s on A100
    # (2026-11-21), far too slow for eval runs. Keep 0; the compile cache volume makes later starts cheaper.
    "SERVE_ENFORCE_EAGER": "0",
}


def _load_settings() -> dict[str, str]:
    values = {name: os.environ.get(name) or default for name, default in SETTINGS.items()}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise SystemExit(
            f"Set {', '.join(missing)} before deploying, e.g.\n"
            "  SERVE_GPU=H100 modal deploy serving/modal_vllm.py\n"
            "GPU options: T4, L4, A10G, L40S, A100-40GB, A100-80GB, H100, H200, B200; add ':N' for N GPUs."
        )
    return values


def gpu_count(gpu: str) -> int:
    """'H100:2' -> 2, 'L40S' -> 1. vLLM splits the model across this many GPUs."""
    _, _, count = gpu.partition(":")
    return int(count) if count else 1


# Locally these come from your shell; inside the container from the image env set below.
S = _load_settings()

hf_cache = modal.Volume.from_name("hf-cache", create_if_missing=True)
vllm_cache = modal.Volume.from_name("vllm-cache", create_if_missing=True)
VOLUMES = {"/root/.cache/huggingface": hf_cache, "/root/.cache/vllm": vllm_cache}

vllm_image = (
    modal.Image.from_registry("nvidia/cuda:12.8.1-devel-ubuntu22.04", add_python="3.12")
    .entrypoint([])
    .uv_pip_install(f"vllm=={VLLM_VERSION}")
    .env(S)
)
download_image = modal.Image.debian_slim(python_version="3.12").uv_pip_install("huggingface_hub").env(S)

app = modal.App("bill-vlm")


@app.function(image=download_image, volumes={"/root/.cache/huggingface": hf_cache}, timeout=60 * MINUTES)
def download() -> None:
    """Fetch the weights into the hf-cache volume, so the first GPU start doesn't pay for the download."""
    from huggingface_hub import snapshot_download

    path = snapshot_download(os.environ["SERVE_MODEL_ID"])
    hf_cache.commit()
    print(f"Weights cached at {path}")


@app.function(
    image=vllm_image,
    gpu=S["SERVE_GPU"],
    volumes=VOLUMES,
    min_containers=0,  # scale to zero: no GPU bill while idle
    max_containers=int(S["SERVE_MAX_CONTAINERS"]),
    scaledown_window=int(S["SERVE_SCALEDOWN_SECONDS"]),
    timeout=20 * MINUTES,  # per request; a 2-page batch on a cold 35B model is well under this
)
@modal.concurrent(max_inputs=8)
@modal.web_server(port=PORT, startup_timeout=20 * MINUTES, requires_proxy_auth=True)
def serve() -> None:
    cmd = [
        "vllm", "serve", os.environ["SERVE_MODEL_ID"],
        "--served-model-name", os.environ["SERVE_MODEL_NAME"],
        "--host", "0.0.0.0",
        "--port", str(PORT),
        "--max-model-len", os.environ["SERVE_MAX_MODEL_LEN"],
        "--tensor-parallel-size", str(gpu_count(os.environ["SERVE_GPU"])),
        "--limit-mm-per-prompt", '{"image": 4}',
        "--gpu-memory-utilization", "0.92",
        # vLLM's default of 256 sequences needs 256 Mamba cache blocks for CUDA-graph capture; Qwen3.6 on one
        # A100-80GB has room for ~114, so startup crashed in a loop (2026-11-21). We never send more than
        # 8 concurrent requests (modal.concurrent below), so 16 is plenty.
        "--max-num-seqs", "16",
        "--uvicorn-log-level", "warning",
    ]
    if os.environ["SERVE_ENFORCE_EAGER"] == "1":
        cmd.append("--enforce-eager")
    print("Starting:", " ".join(cmd))
    subprocess.Popen(cmd)
