"""Score system documents against the answer key (lld.md §11.3).

Line metrics are micro-averaged: counts are summed over documents, then divided.
- line_recall     matched / answer-key lines
- line_precision  matched / system lines
- amount_exact    matched / (matched + misread): of the rows found by name, how many have the right amount
- quantity_match, rate_match   on matched lines where the key has a printed value
- role_accuracy   matched lines whose role equals the key's role
- total_match     complete documents (no partial bill) whose billable sum equals the key's
- ungrounded_rate system lines flagged `ungrounded_value` (not available for the legacy pipeline)
- trusted_precision  `passed` documents that are fully correct / all `passed` documents
- flag_recall     documents with any error that were not `passed` / all documents with any error
A document is fully correct when it has no missed lines, no extra lines and no role errors.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal

from .adapters import ScoredDoc
from .matcher import match_lines

TOL = Decimal("0.011")


@dataclass
class DocScore:
    document_id: str
    split: str
    tags: list[str]
    status: str
    error: str | None
    key_lines: int
    sys_lines: int
    matched: int
    missed: int
    extra: int
    misread: int
    matched_any_page: int
    qty_checked: int
    qty_ok: int
    rate_checked: int
    rate_ok: int
    role_ok: int
    total_checkable: bool
    key_total: float
    sys_total: float
    total_ok: bool | None
    ungrounded: int | None
    correct: bool
    examples: dict = field(default_factory=dict)


def _eq(a: Decimal | None, b: Decimal | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= TOL


def _fmt(ln) -> str:
    amt = "null" if ln.amount is None else f"{ln.amount:,.2f}"
    return f"p{ln.page_no} {ln.item_name[:48]} = {amt} [{ln.role}]"


def score_document(key: ScoredDoc, system: ScoredDoc, split: str, legacy: bool) -> DocScore:
    m = match_lines(key.lines, system.lines)
    any_page = match_lines(key.lines, system.lines, ignore_page=True)
    qty = [(k, s) for k, s in m.matched if k.quantity is not None]
    rate = [(k, s) for k, s in m.matched if k.rate is not None]
    role_ok = sum(k.role == s.role for k, s in m.matched)
    tags = (["handwritten"] if key.is_handwritten else []) + (["partial"] if key.is_partial else [])
    tags = tags or ["printed"]
    checkable = not key.is_partial
    total_ok = _eq(key.billable_sum, system.billable_sum) if checkable and system.status != "failed" else (False if checkable else None)
    correct = system.status != "failed" and not m.missed and not m.extra and role_ok == len(m.matched)
    return DocScore(
        document_id=key.document_id, split=split, tags=tags, status=system.status, error=system.error,
        key_lines=len(key.lines), sys_lines=len(system.lines), matched=len(m.matched), missed=len(m.missed),
        extra=len(m.extra), misread=len(m.misread), matched_any_page=len(any_page.matched),
        qty_checked=len(qty), qty_ok=sum(_eq(k.quantity, s.quantity) for k, s in qty),
        rate_checked=len(rate), rate_ok=sum(_eq(k.rate, s.rate) for k, s in rate),
        role_ok=role_ok, total_checkable=checkable, key_total=float(key.billable_sum),
        sys_total=float(system.billable_sum), total_ok=total_ok,
        ungrounded=None if legacy else sum("ungrounded_value" in ln.flags for ln in system.lines),
        correct=correct,
        examples={
            "missed": [_fmt(x) for x in m.missed[:8]],
            "extra": [_fmt(x) for x in m.extra[:8]],
            "misread": [f"{_fmt(k)}  ->  {s.amount}" for k, s in m.misread[:5]],
            "role_errors": [f"{_fmt(k)} -> system says {s.role}" for k, s in m.matched if k.role != s.role][:5],
        },
    )


def _ratio(n: int | float, d: int | float) -> float | None:
    return None if not d else round(n / d, 4)


def aggregate(scores: list[DocScore]) -> dict:
    s = scores
    tot = lambda f: sum(getattr(x, f) for x in s)  # noqa: E731
    passed = [x for x in s if x.status == "passed"]
    errored = [x for x in s if not x.correct]
    checkable = [x for x in s if x.total_checkable]
    grounded = [x for x in s if x.ungrounded is not None]
    return {
        "documents": len(s),
        "key_lines": tot("key_lines"),
        "system_lines": tot("sys_lines"),
        "line_recall": _ratio(tot("matched"), tot("key_lines")),
        "line_precision": _ratio(tot("matched"), tot("sys_lines")),
        "line_recall_any_page": _ratio(tot("matched_any_page"), tot("key_lines")),
        "amount_exact": _ratio(tot("matched"), tot("matched") + tot("misread")),
        "quantity_match": _ratio(tot("qty_ok"), tot("qty_checked")),
        "rate_match": _ratio(tot("rate_ok"), tot("rate_checked")),
        "role_accuracy": _ratio(tot("role_ok"), tot("matched")),
        "total_match": _ratio(sum(bool(x.total_ok) for x in checkable), len(checkable)),
        "ungrounded_rate": _ratio(sum(x.ungrounded for x in grounded), sum(x.sys_lines for x in grounded)) if grounded else None,
        "documents_correct": sum(x.correct for x in s),
        "documents_failed": sum(x.status == "failed" for x in s),
        "trusted_precision": _ratio(sum(x.correct for x in passed), len(passed)),
        "flag_recall": _ratio(sum(x.status != "passed" for x in errored), len(errored)),
    }


def summarise(scores: list[DocScore]) -> dict:
    out = {"overall": aggregate(scores), "by_split": {}, "by_type": {}}
    for split in sorted({x.split for x in scores}):
        out["by_split"][split] = aggregate([x for x in scores if x.split == split])
    for tag in ("printed", "handwritten", "partial"):
        group = [x for x in scores if tag in x.tags]
        if group:
            out["by_type"][tag] = aggregate(group)
    return out


def as_dict(score: DocScore) -> dict:
    return asdict(score)
