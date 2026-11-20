"""Turn answer-key and system outputs into one shape for scoring.

`ScoredDoc` is deliberately small: the scorer only needs status, lines and bill-level facts.
- `from_contract`: the v2 output contract (answer key v2 and the new pipeline).
- `from_legacy`: the current API response ({is_success, data: {pagewise_line_items}}). It has no roles,
  bills or status, so every line is treated as billable and a successful response as `passed`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

CENT = Decimal("0.01")


def money(x) -> Decimal | None:
    if x is None or x == "":
        return None
    try:
        return Decimal(str(x)).quantize(CENT)
    except (InvalidOperation, ValueError):
        return None


@dataclass
class ScoredLine:
    page_no: int | None
    item_name: str
    amount: Decimal | None
    rate: Decimal | None
    quantity: Decimal | None
    role: str = "billable"
    flags: list[str] = field(default_factory=list)


@dataclass
class ScoredDoc:
    document_id: str
    status: str  # passed | flagged | failed
    lines: list[ScoredLine]
    is_partial: bool = False
    is_handwritten: bool = False
    error: str | None = None

    @property
    def billable_sum(self) -> Decimal:
        return sum((ln.amount for ln in self.lines if ln.role == "billable" and ln.amount is not None), Decimal("0"))


def _page(x) -> int | None:
    try:
        return int(str(x).strip())
    except (TypeError, ValueError):
        return None


def from_contract(doc: dict) -> ScoredDoc:
    lines = [
        ScoredLine(page_no=_page(ln.get("page_no")), item_name=ln.get("item_name") or "",
                   amount=money(ln.get("item_amount")), rate=money(ln.get("item_rate")),
                   quantity=money(ln.get("item_quantity")), role=ln.get("role") or "billable",
                   flags=list(ln.get("flags") or []))
        for b in doc.get("bills", []) for ln in b.get("lines", [])
    ]
    bills = doc.get("bills", [])
    return ScoredDoc(document_id=doc.get("document_id", ""), status=doc.get("status", "failed"), lines=lines,
                     is_partial=any(b.get("is_partial") for b in bills),
                     is_handwritten=any(b.get("is_handwritten") for b in bills) or any(p.get("is_handwritten") for p in doc.get("pages", [])))


def from_legacy(document_id: str, resp: dict) -> ScoredDoc:
    if not resp.get("is_success"):
        return ScoredDoc(document_id=document_id, status="failed", lines=[], error=resp.get("error"))
    lines = []
    for page in (resp.get("data") or {}).get("pagewise_line_items", []):
        for it in page.get("bill_items", []):
            lines.append(ScoredLine(page_no=_page(page.get("page_no")), item_name=it.get("item_name") or "",
                                    amount=money(it.get("item_amount")), rate=money(it.get("item_rate")),
                                    quantity=money(it.get("item_quantity"))))
    return ScoredDoc(document_id=document_id, status="passed", lines=lines)
