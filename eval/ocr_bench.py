"""OCR accuracy benchmark: does the OCR text contain the answer key's numbers on the right page?

OCR is the pipeline's grounding check (every extracted amount must be found in the page's OCR text), so the
measure that matters is: for each answer-key line, is its amount present among the numbers OCR read on
that line's page? Name accuracy is not scored.

    python -m eval.ocr_bench render                      # 15 PDFs -> 200-DPI PNG pages (+ Tesseract, local)
    modal run serving/modal_paddleocr.py --pages eval/outputs/ocr-bench/pages --out eval/outputs/ocr-bench/paddleocr.json
    python -m eval.ocr_bench rapidocr --version PP-OCRv5 --size server   # RapidOCR on CPU (local)
    python -m eval.ocr_bench score                       # report for every engine JSON in the folder

Splits follow eval/splits.yaml: per-document numbers are printed for dev only; test is aggregate only.
Matching is lenient in one way: a number counts as found if it appears anywhere on the page, so two key
lines with the same amount on one page are both found by one occurrence. The same rule applies to every engine.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
PDFS = REPO.parent / "TRAINING_SAMPLES"
KEY = REPO.parent / "TRAINING_SAMPLES_extracted" / "v2"
OUT = REPO / "eval" / "outputs" / "ocr-bench"
DPI = 200  # same as the pipeline (PDF_DPI)

NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str) -> set[float]:
    """Every number OCR read, with thousands separators removed ('1,18,000.00' -> 118000.0)."""
    found = set()
    for token in NUMBER.findall(text):
        token = token.replace(",", "").rstrip(".")
        try:
            found.add(round(float(token), 2))
        except ValueError:
            pass
    return found


def page_name(doc: int, page: int) -> str:
    return f"train_sample_{doc}_p{page:02d}.png"


def render(_args) -> int:
    from pdf2image import convert_from_path
    import pytesseract

    pages_dir = OUT / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    tesseract: dict[str, dict] = {}
    for doc in range(1, 16):
        for i, img in enumerate(convert_from_path(PDFS / f"train_sample_{doc}.pdf", dpi=DPI), start=1):
            name = page_name(doc, i)
            img.save(pages_dir / name)
            start = time.monotonic()
            text = pytesseract.image_to_string(img)  # exactly what the pipeline does today
            tesseract[name] = {"lines": [{"text": t} for t in text.splitlines() if t.strip()],
                               "seconds": round(time.monotonic() - start, 3)}
            print(f"  {name}: tesseract {tesseract[name]['seconds']} s")
    (OUT / "tesseract.json").write_text(json.dumps(tesseract, ensure_ascii=False))
    print(f"{len(tesseract)} pages in {pages_dir}; Tesseract results in {OUT / 'tesseract.json'}")
    return 0


def rapid(args) -> int:
    """RapidOCR (ONNX Runtime, CPU) over the rendered pages, with the chosen PP-OCR version and model size."""
    import numpy as np
    from PIL import Image
    from rapidocr import LangDet, LangRec, ModelType, OCRVersion, RapidOCR

    version, size = OCRVersion(args.version), ModelType(args.size)
    lang_det = LangDet.MULTI if version == OCRVersion.PPOCRV6 else LangDet.CH
    lang_rec = LangRec.MULTI if version == OCRVersion.PPOCRV6 else LangRec.CH
    engine = RapidOCR(params={
        "Det.ocr_version": version, "Det.model_type": size, "Det.lang_type": lang_det,
        "Rec.ocr_version": version, "Rec.model_type": size, "Rec.lang_type": lang_rec,
        "Global.use_cls": True,
        "Global.max_side_len": 4000,  # default 2000 would shrink our 1700x2200 pages; PaddleOCR saw them full size
        "Global.log_level": "warning",
    })
    out: dict[str, dict] = {}
    for path in sorted((OUT / "pages").glob("*.png")):
        img = np.array(Image.open(path).convert("RGB"))
        start = time.monotonic()
        res = engine(img)
        seconds = round(time.monotonic() - start, 3)
        out[path.name] = {"lines": [{"text": t, "score": round(float(s), 4)} for t, s in zip(res.txts or (), res.scores or ())],
                          "seconds": seconds}
        print(f"  {path.name}: {len(out[path.name]['lines'])} lines in {seconds} s")
    name = f"rapidocr-{args.version.lower().replace('-', '')}-{args.size}"
    (OUT / f"{name}.json").write_text(json.dumps(out, ensure_ascii=False))
    print(f"Wrote {OUT / f'{name}.json'}")
    return 0


def key_values(doc: int) -> list[tuple[int, str, float]]:
    """(page_no, field, value) for every non-null amount and rate in the answer key."""
    data = json.loads((KEY / f"train_sample_{doc}.json").read_text())
    out = []
    for bill in data["bills"]:
        for line in bill["lines"]:
            for field in ("item_amount", "item_rate"):
                if line.get(field) is not None:
                    out.append((line["page_no"], field, round(float(line[field]), 2)))
    return out


def score_engine(results: dict[str, dict], splits: dict[str, list[int]]) -> dict:
    report = {}
    for split, docs in splits.items():
        tot = {"item_amount": [0, 0, 0], "item_rate": [0, 0, 0]}  # found on page, found in doc, total
        per_doc, seconds, n_pages = {}, 0.0, 0
        for doc in docs:
            pages = {int(n.split("_p")[1][:2]): r for n, r in results.items() if n.startswith(f"train_sample_{doc}_p")}
            nums = {p: numbers_in("\n".join(l["text"] for l in r["lines"])) for p, r in pages.items()}
            doc_nums = set().union(*nums.values()) if nums else set()
            seconds += sum(r.get("seconds", 0) for r in pages.values())
            n_pages += len(pages)
            d = {"item_amount": [0, 0, 0], "item_rate": [0, 0, 0]}
            for page, field, value in key_values(doc):
                d[field][2] += 1
                d[field][0] += value in nums.get(page, set())
                d[field][1] += value in doc_nums
            for f in d:
                for k in range(3):
                    tot[f][k] += d[f][k]
            per_doc[doc] = d
        report[split] = {"totals": tot, "per_doc": per_doc, "sec_per_page": seconds / n_pages if n_pages else None,
                         "pages": n_pages}
    return report


def pct(a: int, b: int) -> str:
    return f"{100 * a / b:5.1f}%" if b else "  n/a"


def score(_args) -> int:
    splits = yaml.safe_load((REPO / "eval" / "splits.yaml").read_text())
    engines = sorted(OUT.glob("*.json"))
    if not engines:
        print(f"No engine results in {OUT}. Run `render` and the PaddleOCR command first.")
        return 1
    lines = ["# OCR benchmark: answer-key numbers found by OCR", "",
             "Share of answer-key values whose number appears in the OCR text of the right page "
             "(and anywhere in the document, for comparison). 200-DPI renders of the 15 training bills.", "",
             "| Engine | Split | Amounts on right page | Amounts anywhere in doc | Rates on right page | Sec/page |",
             "|---|---|---|---|---|---|"]
    all_reports = {}
    for path in engines:
        rep = score_engine(json.loads(path.read_text()), splits)
        all_reports[path.stem] = rep
        for split in ("dev", "test"):
            t = rep[split]["totals"]
            sp = rep[split]["sec_per_page"]
            lines.append(f"| {path.stem} | {split} ({rep[split]['pages']} pages) | {pct(*t['item_amount'][::2])} "
                         f"({t['item_amount'][0]}/{t['item_amount'][2]}) | {pct(t['item_amount'][1], t['item_amount'][2])} "
                         f"| {pct(*t['item_rate'][::2])} | {sp:.2f} |" if sp is not None else "")
    lines += ["", "## Dev split, amounts on the right page, per document", "",
              "| Doc | " + " | ".join(all_reports) + " |", "|---" * (len(all_reports) + 1) + "|"]
    for doc in splits["dev"]:
        cells = []
        for rep in all_reports.values():
            a = rep["dev"]["per_doc"][doc]["item_amount"]
            cells.append(f"{pct(a[0], a[2])} ({a[0]}/{a[2]})")
        lines.append(f"| {doc} | " + " | ".join(cells) + " |")
    text = "\n".join(lines) + "\n"
    report_path = REPO / "eval" / "reports" / f"{time.strftime('%Y-%m-%d')}_ocr-bench.md"
    report_path.write_text(text)
    print(text)
    print(f"Report: {report_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("render").set_defaults(fn=render)
    r = sub.add_parser("rapidocr", help="RapidOCR on CPU over the rendered pages")
    r.add_argument("--version", default="PP-OCRv5", choices=("PP-OCRv4", "PP-OCRv5", "PP-OCRv6"))
    r.add_argument("--size", default="server", choices=("mobile", "server", "tiny", "small", "medium"))
    r.set_defaults(fn=rapid)
    sub.add_parser("score").set_defaults(fn=score)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
