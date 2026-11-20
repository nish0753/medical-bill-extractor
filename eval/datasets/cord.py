"""Convert the CORD v2 test split (naver-clova-ix/cord-v2, CC-BY-4.0) into answer-key v2 documents.

    python -m eval.datasets.cord --parquet ../EXTERNAL_DATASETS/cord/raw/test.parquet --out ../EXTERNAL_DATASETS/cord

Writes <out>/images/cord_test_NNN.<ext>, <out>/key_v2/cord_test_NNN.json and <out>/manifest.json.

CORD is a receipt dataset (Indonesian shops, rupiah), used here as an out-of-domain check of line-item
extraction. Mapping: menu item -> one `billable` line on page 1; nm -> item_name, price -> item_amount,
unitprice -> item_rate, cnt -> item_quantity; total.total_price -> printed grand total.
It has no summary pages or parent/child rows, so it measures line recall, precision and amounts, not roles.

Excluded receipts (listed in manifest.json with the reason):
- any item with priced sub-items: whether a sub-item's price is inside its parent's or added to it varies
  by shop and isn't labelled, so neither role can be scored honestly;
- any item with no printed price, or a price that can't be parsed unambiguously.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq

THOUSANDS = re.compile(r"^\d{1,3}([.,]\d{3})+$")          # 60.000  25,000  1,591,600
DECIMAL = re.compile(r"^\d+([.,]\d{3})*[.,]\d{2}$")         # 21000.00  1,234.50  9.999,99
PLAIN = re.compile(r"^\d+$")


def parse_rupiah(raw: str | None) -> Decimal | None:
    """Parse a CORD price string; None if absent or ambiguous."""
    if raw is None:
        return None
    s = re.sub(r"(?i)^rp\.?\s*", "", str(raw).strip()).lstrip("*").strip()
    neg = s.startswith("-")
    s = s.lstrip("-").strip()
    if PLAIN.match(s):
        v = Decimal(s)
    elif THOUSANDS.match(s):
        v = Decimal(re.sub(r"[.,]", "", s))
    elif DECIMAL.match(s):
        whole, frac = s[:-3], s[-2:]
        v = Decimal(re.sub(r"[.,]", "", whole) + "." + frac)
    else:
        return None
    return -v if neg else v


def parse_count(raw) -> Decimal | None:
    if raw is None:
        return None
    m = re.match(r"^\s*(\d+(?:[.,]\d+)?)\s*[xX]?\s*$", str(raw))
    return Decimal(m.group(1).replace(",", ".")) if m else None


def as_list(x) -> list:
    return [] if x is None else ([x] if isinstance(x, dict) else list(x))


def num(x: Decimal | None):
    return None if x is None else float(x)


def convert(gt: dict, doc_id: str, image_name: str) -> tuple[dict | None, str | None]:
    parse = gt.get("gt_parse", {})
    lines = []
    for n, item in enumerate(as_list(parse.get("menu")), 1):
        if any(sub.get("price") for sub in as_list(item.get("sub"))):
            return None, "item has priced sub-items (role not labelled)"
        if isinstance(item.get("price"), list) or isinstance(item.get("nm"), list):
            return None, "item has several values in one field"
        amount = parse_rupiah(item.get("price"))
        if amount is None:
            return None, f"item price missing or ambiguous: {item.get('price')!r}"
        lines.append({
            "line_id": f"b1-{n:04d}", "page_no": 1, "section": None, "date": None,
            "item_name": str(item.get("nm", "")).strip(), "item_quantity": num(parse_count(item.get("cnt"))),
            "item_rate": num(parse_rupiah(item.get("unitprice"))), "item_amount": num(amount),
            "role": "billable", "covered_by": [], "parent_line_id": None, "flags": [],
            "gross_amount": None, "discount_amount": None,
        })
    if not lines:
        return None, "no menu items"
    total = parse_rupiah((parse.get("total") or {}).get("total_price")) if isinstance((parse.get("total") or {}).get("total_price"), str) else None
    doc = {
        "schema_version": 2, "document_id": doc_id, "source_file": image_name, "page_count": 1, "status": "passed",
        "pages": [{"page_no": 1, "page_type": "detail", "is_handwritten": False}],
        "bills": [{
            "bill_id": "b1", "label": "CORD receipt", "page_nos": [1], "is_partial": False, "is_handwritten": False,
            "printed": {"subtotal": None, "discount": None, "tax": None, "round_off": None, "grand_total": num(total)},
            "tax_mode": "unknown", "extracted": {"billable_sum": num(sum(Decimal(str(ln["item_amount"])) for ln in lines))},
            "reconciliation": {"status": "not_checkable", "difference": None,
                               "reason": "receipt totals include taxes, service and discounts not labelled per line"},
            "sections": [], "lines": lines,
        }],
    }
    return doc, None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Convert CORD v2 test split to answer-key v2")
    ap.add_argument("--parquet", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    table = pq.read_table(args.parquet)
    (args.out / "images").mkdir(parents=True, exist_ok=True)
    (args.out / "key_v2").mkdir(parents=True, exist_ok=True)
    manifest = {"source": "naver-clova-ix/cord-v2 test split (CC-BY-4.0)", "included": [], "excluded": {}}
    for i in range(table.num_rows):
        doc_id = f"cord_test_{i:03d}"
        image = table.column("image")[i].as_py()
        data = image["bytes"]
        ext = ".png" if data[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"
        gt = json.loads(table.column("ground_truth")[i].as_py())
        doc, reason = convert(gt, doc_id, doc_id + ext)
        if doc is None:
            manifest["excluded"][doc_id] = reason
            continue
        (args.out / "images" / (doc_id + ext)).write_bytes(data)
        (args.out / "key_v2" / f"{doc_id}.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        manifest["included"].append(doc_id)
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    lines = sum(len(json.loads((args.out / "key_v2" / f"{d}.json").read_text())["bills"][0]["lines"]) for d in manifest["included"])
    print(f"included {len(manifest['included'])} receipts ({lines} lines), excluded {len(manifest['excluded'])}")
    for d, r in manifest["excluded"].items():
        print(f"  {d}: {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
