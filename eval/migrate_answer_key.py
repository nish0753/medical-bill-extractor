"""M0 migration: build answer key v2 (every printed line, each with a role) from the hand transcriptions.

Source: ../TRAINING_SAMPLES_extracted/source/ (data1-4.py, children13.py). These hold the bill structure
(bills, pages, summary pages, parent/child rows) that the v1 JSON files flattened away.
Output: ../TRAINING_SAMPLES_extracted/v2/train_sample_N.json, in the output contract of project-overview.md.

Run:  python -m eval.migrate_answer_key            (writes v2, then checks it against v1)
      python -m eval.migrate_answer_key --check    (checks only, writes nothing)

The script refuses to write if any check fails: v2 must keep v1's totals and pass the bill's own arithmetic
exactly where v1 did.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

from pypdf import PdfReader

REPO = Path(__file__).resolve().parents[1]
KEY_ROOT = REPO.parent / "TRAINING_SAMPLES_extracted"
SOURCE = KEY_ROOT / "source"
OUT = KEY_ROOT / "v2"
PDFS = REPO.parent / "TRAINING_SAMPLES"

CENT = Decimal("0.01")
TOL = Decimal("0.011")


def D(x) -> Decimal | None:
    return None if x is None else Decimal(str(x)).quantize(CENT)


def num(x: Decimal | None):
    return None if x is None else float(x)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SOURCE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------------------------
# Facts that the transcriptions don't encode, each read from the page images.
# ---------------------------------------------------------------------------------------------

# Tax mode per (sample, bill index). Not listed = no tax printed ("unknown").
TAX_MODE = {(2, 0): "inclusive", (2, 1): "inclusive", (2, 2): "inclusive",
            (5, 0): "exclusive", (5, 1): "inclusive", (5, 2): "inclusive"}

# Round-off sign as it affects the total (the transcriptions store magnitudes).
ROUND_OFF_SIGN = {(5, 1): -1}

# Sample 4: "Discount 3057.00" is printed below the Grand Total with the deposit and refund. It is a
# settlement figure, not part of the arithmetic that reaches the Grand Total.
SETTLEMENT_DISCOUNT = {(4, 0)}

# Counting rule R1. Samples 4 and 10: the detail pages are incomplete, so the summary page's category
# lines are billable and the detail lines are breakdowns of a category.
DETAIL_CATEGORY = {
    4: lambda name: "Consultation" if "consultation" in name.lower() else "Drugs",
    10: lambda name: "Consultation",
}

# Sample 14: the main bill's PHARMACY CHARGES line is itemised in full by bill 2 (pages 4-6), which adds
# up to it exactly, so it is a summary line covered by bill 2.
SUMMARY_LINES = {(14, 0, "PHARMACY CHARGES (subtotal; itemised on pages 4-6)"): ("PHARMACY CHARGES", "b2")}

# Sections with printed subtotals, as index ranges into the bill's lines in print order (top-level rows
# only). Taken from the v1 build checks. Missing pages make a section not checkable.
SECTIONS = {
    (1, 0): [("Radiological Investigation", 0, 3), ("BED CHARGES", 3, 7), ("CONSULTATION", 7, 11),
             ("PATHOLOGY", 11, 37), ("PHARMACY CHARGE", 37, 38)],
    (4, 0): [("Consultation", 8, 12)],
    (10, 0): [("Consultation", 16, 28, "not_checkable")],  # rows continue on pages 4-90, not in this file
    (12, 0): [("Equipment/Instrument", 25, 29)],
    (14, 0): [("Room", 0, 1), ("Admission", 1, 2), ("Consultation", 2, 7), ("Laboratory", 7, 37),
              ("Ultrasound", 37, 38), ("CT", 38, 39), ("X-ray", 39, 40), ("Procedure", 40, 41),
              ("Surgical", 41, 43), ("Anaesthetist", 43, 44), ("OT", 44, 45), ("Blood bank", 45, 47),
              ("Equipment", 47, 51), ("Hospital", 51, 55), ("Pharmacy", 55, 56), ("Diet", 56, 57),
              ("Billing", 57, 60), ("MRD", 60, 61)],
}
# Sample 12, page 3: "Surgical Disposables" are the first 3 rows of that page. Sample 15 sections are
# given as page slices in data4; both are converted to index ranges at run time.
PAGE_SECTIONS = {
    (12, 0): [("Surgical Disposables", [(2, 0, 3)])],
    (15, 0): [("ADMISSION CHARGES", [(0, 0, 1)]), ("WARD CHARGES", [(0, 1, 15)]),
              ("NURSING CHARGES", [(0, 15, None), (1, 0, 8)]), ("MINOR PROCEDURE", [(1, 8, 19)]),
              ("LABORATORY", [(1, 19, None), (2, 0, 20)]), ("RADIOLOGY", [(2, 20, None), (3, 0, 1)]),
              ("DOCTOR VISITS", [(3, 1, 4)]), ("CHARGES (p4-p8)", [(3, 4, None), (4, 0, None), (5, 0, None),
                                                                     (6, 0, None), (7, 0, -1)])],
}
# Printed subtotals that the transcriptions only used in checks.
EXTRA_SECTION_TOTALS = {4: {"Consultation": 2650.00},
                        12: {"Equipment/Instrument": 2070.00, "Surgical Disposables": 711.15},
                        14: {"Room": 45000, "Admission": 100, "Consultation": 27000, "Laboratory": 23030,
                             "Ultrasound": 3000, "CT": 9500, "X-ray": 450, "Procedure": 5000, "Surgical": 63000,
                             "Anaesthetist": 18000, "OT": 10500, "Blood bank": 2900, "Equipment": 12000,
                             "Hospital": 2100, "Pharmacy": 54736.69, "Diet": 510, "Billing": 19000, "MRD": 200}}

ANNOTATION = re.compile(r"\s*\((handwritten, uncertain)\)\s*$")

PAGE_TYPE = {"Bill Detail": "detail", "Pharmacy": "pharmacy", "Final Bill": "summary"}


# ---------------------------------------------------------------------------------------------


def _page_count(sample: int) -> int:
    return len(PdfReader(str(PDFS / f"train_sample_{sample}.pdf")).pages)


def _resolve_page_sections(sample: int, bill: dict) -> list[tuple]:
    pages = bill["pages"]
    offsets, n = [], 0
    for p in pages:
        offsets.append(n)
        n += len(p["items"])
    out = []
    for name, parts in PAGE_SECTIONS.get((sample, 0), []):
        idx = []
        for page_i, start, stop in parts:
            size = len(pages[page_i]["items"])
            stop = size if stop is None else (size + stop if stop < 0 else stop)
            idx.extend(range(offsets[page_i] + start, offsets[page_i] + stop))
        out.append((name, idx))
    return out


def build_document(sample: int, src: dict, children13) -> dict:
    page_count = _page_count(sample)
    pages_meta: dict[int, dict] = {}
    bills_out = []
    for b_i, bill in enumerate(src["bills"]):
        bill_id = f"b{b_i + 1}"
        handwritten = bool(bill.get("handwritten"))
        lines: list[dict] = []  # (order key, line)
        top_level: list[dict] = []  # rows of the bill's own table, in print order (for sections)
        seq = 0

        def add(page_no: int, name: str, amount, rate, qty, role: str, **extra) -> dict:
            nonlocal seq
            flags = []
            clean = ANNOTATION.sub("", name)
            if clean != name or (handwritten and role != "breakdown" and "?" in name):
                flags.append("handwritten_uncertain")
            amt = D(amount)
            if amt is not None and amt < 0:
                flags.append("negative_return")
            line = {"page_no": page_no, "section": None, "date": None, "item_name": clean,
                    "item_quantity": num(D(qty)), "item_rate": num(D(rate)), "item_amount": num(amt),
                    "role": role, "covered_by": [], "parent_line_id": None, "flags": sorted(set(flags)),
                    "gross_amount": None, "discount_amount": None, "_seq": seq}
            line.update(extra)
            seq += 1
            lines.append(line)
            return line

        summary_by_category: dict[str, dict] = {}
        for p in bill["pages"]:
            page_no = int(p["page_no"])
            meta = pages_meta.setdefault(page_no, {"page_no": page_no, "page_type": None, "is_handwritten": False})
            ptype = "summary" if p.get("is_summary") else PAGE_TYPE[p["page_type"]]
            if not p["items"] and p["page_type"] == "Final Bill":
                ptype = "totals_only"
            meta["page_type"] = meta["page_type"] or ptype
            meta["is_handwritten"] = meta["is_handwritten"] or handwritten

            for idx, (name, amount, rate, qty) in enumerate(p["items"]):
                key13 = (page_no, idx)
                if sample == 13 and key13 in children13.PARENT_NAMES:
                    name = children13.PARENT_NAMES[key13]
                if sample == 13 and name.startswith("IPD CONSUMABLE CHARGES") and "(implants)" in name:
                    name = name.replace(" (implants)", "")

                role, extra = "billable", {}
                if sample in DETAIL_CATEGORY:
                    if p.get("is_summary"):
                        role = "billable"
                    else:
                        role = "breakdown"
                        extra["_category"] = DETAIL_CATEGORY[sample](name)
                summary = SUMMARY_LINES.get((sample, b_i, name))
                if summary:
                    name, covered = summary
                    role, extra = "summary", {"covered_by": [covered]}

                line = add(page_no, name, amount, rate, qty, role, **extra)
                top_level.append(line)
                if p.get("is_summary"):
                    summary_by_category[name] = line

                if sample == 13:
                    for c_page, c_name, c_qty, c_amt in children13.CHILDREN.get(key13, []):
                        child = add(c_page, c_name, c_amt, None, c_qty, "breakdown")
                        child["_parent"] = line

        # Order lines as printed: by page, then emission order (children that run onto the next page
        # come before that page's own rows, because their parent was emitted earlier).
        lines.sort(key=lambda ln: (ln["page_no"], ln["_seq"]))
        for n, line in enumerate(lines, 1):
            line["line_id"] = f"{bill_id}-{n:04d}"
        for line in lines:
            if "_parent" in line:
                line["parent_line_id"] = line["_parent"]["line_id"]
                line["covered_by"] = [line["_parent"]["line_id"]]
            if "_category" in line:
                line["covered_by"] = [summary_by_category[line["_category"]]["line_id"]]

        # Sections
        sections = []
        specs = [(name, list(range(a, b)), (rest[0] if rest else None))
                 for name, a, b, *rest in SECTIONS.get((sample, b_i), [])]
        if b_i == 0:
            specs += [(name, idx, None) for name, idx in _resolve_page_sections(sample, bill)]
        totals = dict(bill.get("section_totals") or {})
        totals.update(EXTRA_SECTION_TOTALS.get(sample, {}))
        for name, idx, forced in specs:
            members = [top_level[i] for i in idx]
            extracted = sum((D(m["item_amount"]) for m in members), Decimal("0"))
            printed = D(totals[name])
            for m in members:
                m["section"] = name
            status = forced or ("passed" if abs(extracted - printed) <= TOL else "mismatch")
            sections.append({"name": name, "printed_subtotal": num(printed),
                             "extracted_sum": num(extracted), "status": status})

        # Printed totals and reconciliation.
        # The bill total is checked against the bill's top-level lines: billable + summary. Breakdown
        # lines are never added, because their parent or summary line already carries the money.
        sign = ROUND_OFF_SIGN.get((sample, b_i), 1)
        round_off = D(bill.get("roundoff"))
        round_off = None if round_off is None else round_off * sign
        discount = None if (sample, b_i) in SETTLEMENT_DISCOUNT else D(bill.get("discount"))
        printed = {"subtotal": num(D(bill.get("printed_subtotal"))), "discount": num(discount),
                   "tax": num(D(bill.get("tax"))), "round_off": num(round_off),
                   "grand_total": num(D(bill.get("printed_total")))}
        tax_mode = TAX_MODE.get((sample, b_i), "unknown")
        billable_sum = sum((D(ln["item_amount"]) for ln in lines if ln["role"] == "billable"), Decimal("0"))
        top_sum = sum((D(ln["item_amount"]) for ln in lines if ln["role"] in ("billable", "summary")), Decimal("0"))
        recon = reconcile(top_sum, printed, tax_mode)

        page_nos = sorted({int(p["page_no"]) for p in bill["pages"]})
        for line in lines:
            for k in [k for k in line if k.startswith("_")]:
                del line[k]
        bills_out.append({
            "bill_id": bill_id, "label": bill["bill"], "page_nos": page_nos,
            "is_partial": bool(bill.get("partial")), "is_handwritten": handwritten,
            "printed": printed, "tax_mode": tax_mode,
            "extracted": {"billable_sum": num(billable_sum)},
            "reconciliation": recon, "sections": sections, "lines": lines,
        })

    for n in range(1, page_count + 1):
        pages_meta.setdefault(n, {"page_no": n, "page_type": "other", "is_handwritten": False})
    return {
        "schema_version": 2,
        "document_id": f"train_sample_{sample}",
        "source_file": f"train_sample_{sample}.pdf",
        "page_count": page_count,
        "status": "passed",
        "pages": [pages_meta[n] for n in sorted(pages_meta)],
        "bills": bills_out,
    }


def reconcile(top_sum: Decimal, printed: dict, tax_mode: str) -> dict:
    total = D(printed["grand_total"])
    if total is None:
        return {"status": "not_checkable", "difference": None, "reason": "no printed total in this file"}
    expected = top_sum
    if tax_mode == "exclusive" and printed["tax"] is not None:
        expected += D(printed["tax"])
    if printed["discount"] is not None:
        expected -= D(printed["discount"])
    if printed["round_off"] is not None:
        expected += D(printed["round_off"])
    diff = expected - total
    if abs(diff) <= TOL:
        return {"status": "passed", "difference": num(diff), "reason": None}
    if printed["round_off"] is None and total == total.to_integral_value() and abs(diff) < Decimal("0.5"):
        return {"status": "passed", "difference": num(diff), "reason": "total rounded to the rupee, no round-off line printed"}
    return {"status": "mismatch", "difference": num(diff),
            "reason": f"lines give {expected:,.2f}, bill prints {total:,.2f}"}


# ---------------------------------------------------------------------------------------------


def check_against_v1(sample: int, doc: dict, children13) -> list[str]:
    """v2 must preserve every v1 line and v1's counted total; return a list of problems."""
    problems = []
    v1 = json.loads((KEY_ROOT / f"train_sample_{sample}.json").read_text())
    lines = [ln for b in doc["bills"] for ln in b["lines"]]

    billable = sum((D(ln["item_amount"]) for ln in lines if ln["role"] == "billable"), Decimal("0"))
    if billable != D(v1["sum_of_extracted_item_amounts"]):
        problems.append(f"billable sum {billable} != v1 {v1['sum_of_extracted_item_amounts']}")

    n_children = sum(len(v) for v in children13.CHILDREN.values()) if sample == 13 else 0
    v1_count = v1["data"]["total_item_count"] + len(v1["lines_not_counted_to_avoid_double_counting"])
    if len(lines) != v1_count + n_children:
        problems.append(f"{len(lines)} lines != v1 {v1_count} + {n_children} new breakdown rows")

    by_id = {ln["line_id"]: ln for ln in lines}
    for ln in lines:
        if ln["role"] not in ("billable", "summary", "breakdown"):
            problems.append(f"{ln['line_id']}: bad role {ln['role']}")
        if ln["role"] != "billable" and not ln["covered_by"]:
            problems.append(f"{ln['line_id']}: {ln['role']} line without covered_by")
    # Every parent's breakdown rows add up to it (counting rule R2).
    kids: dict[str, list] = {}
    for ln in lines:
        if ln["parent_line_id"]:
            kids.setdefault(ln["parent_line_id"], []).append(D(ln["item_amount"]))
    for pid, amounts in kids.items():
        if abs(abs(D(by_id[pid]["item_amount"])) - sum(amounts)) > TOL:
            problems.append(f"{pid}: children sum {sum(amounts)} != parent {by_id[pid]['item_amount']}")
    # Summary lines equal what covers them (counting rule R1).
    bills = {b["bill_id"]: b for b in doc["bills"]}
    for ln in lines:
        if ln["role"] == "summary":
            covered = sum((D(x["item_amount"]) for x in bills[ln["covered_by"][0]]["lines"] if x["role"] == "billable"), Decimal("0"))
            if covered != D(ln["item_amount"]):
                problems.append(f"{ln['line_id']}: summary {ln['item_amount']} != covering bill {covered}")
    return problems


EXPECTED_RECONCILIATION = {  # (sample, bill_id) -> status; anything else must be "passed" or "not_checkable"
    (14, "b1"): "mismatch",  # the bill's own Rs 262.69 error (counting rule R8)
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="validate only; write nothing")
    args = ap.parse_args()

    S = {}
    for name in ("data1", "data2", "data3", "data4"):
        S.update(_load(name).S)
    children13 = _load("children13")

    docs, problems = {}, []
    for sample in sorted(S):
        doc = build_document(sample, S[sample], children13)
        docs[sample] = doc
        problems += [f"sample {sample}: {p}" for p in check_against_v1(sample, doc, children13)]
        for b in doc["bills"]:
            status = b["reconciliation"]["status"]
            want = EXPECTED_RECONCILIATION.get((sample, b["bill_id"]))
            if want and status != want:
                problems.append(f"sample {sample} {b['bill_id']}: reconciliation {status}, expected {want}")
            if not want and status == "mismatch":
                problems.append(f"sample {sample} {b['bill_id']}: unexpected mismatch: {b['reconciliation']['reason']}")
            for s in b["sections"]:
                if s["status"] == "mismatch":
                    problems.append(f"sample {sample} {b['bill_id']} section {s['name']}: {s['extracted_sum']} vs {s['printed_subtotal']}")

    print(f"{'sample':>6} {'pages':>5} {'lines':>5} {'billable':>8} {'summary':>7} {'breakdown':>9}  bills (reconciliation)")
    for sample, doc in docs.items():
        lines = [ln for b in doc["bills"] for ln in b["lines"]]
        roles = {r: sum(ln["role"] == r for ln in lines) for r in ("billable", "summary", "breakdown")}
        recon = ", ".join(f"{b['bill_id']} {b['reconciliation']['status']}" for b in doc["bills"])
        print(f"{sample:>6} {doc['page_count']:>5} {len(lines):>5} {roles['billable']:>8} {roles['summary']:>7} {roles['breakdown']:>9}  {recon}")

    if problems:
        print("\nPROBLEMS (nothing written):")
        for p in problems:
            print("  " + p)
        return 1
    if not args.check:
        OUT.mkdir(exist_ok=True)
        for sample, doc in docs.items():
            (OUT / f"train_sample_{sample}.json").write_text(json.dumps(doc, indent=2) + "\n")
        print(f"\nWrote {len(docs)} documents to {OUT}")
    else:
        print("\nAll checks passed (--check: nothing written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
