"""PaddleOCR (PP-OCRv5 server models) on a Modal GPU, for the OCR accuracy benchmark (eval/ocr_bench.py).
Offline experiment only; the app never calls this.

    modal run serving/modal_paddleocr.py --pages eval/outputs/ocr-bench/pages --out eval/outputs/ocr-bench/paddleocr.json

Reads every *.png in --pages, OCRs it on the GPU, and writes {page_file: {"lines": [{text, score, box}], "seconds"}}.
Runs as an ephemeral app: the GPU exists only for the duration of the command.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import modal

PADDLE_VERSION = "3.3.1"  # paddlepaddle-gpu, from Paddle's official CUDA 12.6 wheel index (not on PyPI)
PADDLEOCR_VERSION = "3.7.0"
GPU = "L4"  # 24 GB; OCR models are small, so a cheap GPU is enough

image = (
    modal.Image.from_registry("nvidia/cuda:12.6.3-cudnn-runtime-ubuntu22.04", add_python="3.12")
    .entrypoint([])
    .apt_install("libgl1", "libglib2.0-0", "libgomp1")
    .pip_install(f"paddlepaddle-gpu=={PADDLE_VERSION}",
                 index_url="https://www.paddlepaddle.org.cn/packages/stable/cu126/")
    .pip_install(f"paddleocr=={PADDLEOCR_VERSION}")
)
models = modal.Volume.from_name("paddle-models", create_if_missing=True)  # PaddleX model downloads, kept between runs

app = modal.App("paddle-ocr-bench")


@app.cls(image=image, gpu=GPU, volumes={"/root/.paddlex": models}, timeout=30 * 60, max_containers=1)
class Ocr:
    @modal.enter()
    def load(self) -> None:
        from paddleocr import PaddleOCR

        self.ocr = PaddleOCR(
            text_detection_model_name="PP-OCRv5_server_det",
            text_recognition_model_name="PP-OCRv5_server_rec",
            use_doc_orientation_classify=True,  # sideways scans (sample 3)
            use_doc_unwarping=False,  # flat scans; unwarping can distort table rows
            use_textline_orientation=True,
            device="gpu",
        )
        models.commit()

    @modal.method()
    def run(self, name: str, png: bytes) -> tuple[str, dict]:
        import numpy as np
        import cv2

        img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        start = time.monotonic()
        result = self.ocr.predict(img)[0]
        seconds = round(time.monotonic() - start, 3)
        lines = [
            {"text": text, "score": round(float(score), 4), "box": [int(v) for v in box]}
            for text, score, box in zip(result["rec_texts"], result["rec_scores"], result["rec_boxes"])
        ]
        return name, {"lines": lines, "seconds": seconds}


@app.local_entrypoint()
def main(pages: str, out: str) -> None:
    files = sorted(Path(pages).glob("*.png"))
    if not files:
        raise SystemExit(f"No .png pages in {pages}")
    print(f"OCR on {len(files)} pages, GPU {GPU}")
    results = {}
    for name, data in Ocr().run.starmap(((f.name, f.read_bytes()) for f in files), order_outputs=False):
        results[name] = data
        print(f"  {name}: {len(data['lines'])} lines in {data['seconds']} s")
    Path(out).write_text(json.dumps(results, ensure_ascii=False))
    print(f"Wrote {out}")
