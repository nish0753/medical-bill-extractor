"""Score a folder of system outputs against answer key v2 and write a report (lld.md §11.3, §11.5).

    python -m eval.run_eval --outputs eval/outputs/<run> --format legacy|contract --label <name>
        [--save-baseline]   copy this report's JSON to eval/baselines/latest.json (only after a milestone is accepted)
        [--compare]         regression gate: exit 2 if dev line recall or trusted precision drops below the baseline

    python -m eval.run_eval ... --key-dir ../EXTERNAL_DATASETS/cord/key_v2   (an external dataset, split "external")

Outputs are files named <document_id>.json. A missing file scores as a failed document.
Reports go to eval/reports/{date}_{label}.md and .json. Per-document detail is shown for the dev split only;
the test split is reported as aggregates, so nobody tunes against it.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import yaml

from .adapters import ScoredDoc, from_contract, from_legacy
from .metrics import as_dict, score_document, summarise

EVAL = Path(__file__).resolve().parent
KEY_DIR = EVAL.parents[1] / "TRAINING_SAMPLES_extracted" / "v2"
BASELINE = EVAL / "baselines" / "latest.json"
GATE_METRICS = ("line_recall", "trusted_precision")

METRIC_ROWS = [
    ("line_recall", "Line recall"), ("line_precision", "Line precision"),
    ("line_recall_any_page", "Line recall ignoring page numbers (diagnostic)"),
    ("amount_exact", "Amount exact match"), ("quantity_match", "Quantity match"), ("rate_match", "Rate match"),
    ("role_accuracy", "Role accuracy"), ("total_match", "Billable total match (complete documents)"),
    ("ungrounded_rate", "Ungrounded rate"), ("trusted_precision", "Trusted precision"),
    ("flag_recall", "Flag recall"),
]


def load_splits() -> dict[int, str]:
    raw = yaml.safe_load((EVAL / "splits.yaml").read_text())
    return {n: split for split, ns in raw.items() for n in ns}


def load_system(path: Path, doc_id: str, fmt: str) -> ScoredDoc:
    if not path.exists():
        return ScoredDoc(document_id=doc_id, status="failed", lines=[], error="no output file")
    data = json.loads(path.read_text())
    return from_legacy(doc_id, data) if fmt == "legacy" else from_contract(data)


def pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def render_markdown(label: str, outputs: Path, summary: dict, docs: list[dict], gate: list[str] | None) -> str:
    md = [f"# Evaluation: {label}", "", f"- Date: {date.today().isoformat()}", f"- Outputs: `{outputs}`",
          f"- Answer key: v2 ({summary['overall']['key_lines']} lines in {summary['overall']['documents']} documents)", ""]
    cols = [c for c in ("dev", "test", "external") if c in summary["by_split"]] + ["overall"]
    groups = {**summary["by_split"], "overall": summary["overall"]}
    md += ["## Metrics", "", "| Metric | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for key, name in METRIC_ROWS:
        md.append(f"| {name} | " + " | ".join(pct(groups.get(c, {}).get(key)) for c in cols) + " |")
    for key, name in [("documents", "Documents"), ("documents_correct", "Documents fully correct"),
                      ("documents_failed", "Documents failed"), ("key_lines", "Answer-key lines"),
                      ("system_lines", "System lines")]:
        md.append(f"| {name} | " + " | ".join(str(groups.get(c, {}).get(key, "")) for c in cols) + " |")
    md += ["", "## By bill type (all splits)", "", "| Type | Docs | Line recall | Line precision | Role accuracy | Total match |",
           "|---|---|---|---|---|---|"]
    for tag, agg in summary["by_type"].items():
        md.append(f"| {tag} | {agg['documents']} | {pct(agg['line_recall'])} | {pct(agg['line_precision'])} | "
                  f"{pct(agg['role_accuracy'])} | {pct(agg['total_match'])} |")
    if gate is not None:
        md += ["", "## Regression gate (dev vs eval/baselines/latest.json)", ""] + [f"- {g}" for g in gate]
    dev = [d for d in docs if d["split"] != "test"]
    md += ["", "## Documents (not shown for the test split)", "", "| Document | Type | Status | Key | System | Matched | Missed | Extra | Misread | Roles ok | Key total | System total |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for d in dev:
        md.append(f"| {d['document_id']} | {', '.join(d['tags'])} | {d['status']} | {d['key_lines']} | {d['sys_lines']} | "
                  f"{d['matched']} | {d['missed']} | {d['extra']} | {d['misread']} | {d['role_ok']}/{d['matched']} | "
                  f"{d['key_total']:,.2f} | {d['sys_total']:,.2f} |")
    md += ["", "### Examples", ""]
    for d in dev:
        if d["correct"]:
            continue
        md.append(f"**{d['document_id']}**" + (f" (error: {d['error']})" if d["error"] else ""))
        for kind in ("missed", "extra", "misread", "role_errors"):
            for e in d["examples"][kind]:
                md.append(f"- {kind}: {e}")
        md.append("")
    md += ["", "Test documents are scored in aggregate only (lld.md §11.1)."]
    return "\n".join(md) + "\n"


def regression_gate(summary: dict) -> tuple[list[str], bool]:
    if not BASELINE.exists():
        return ["No baseline yet (eval/baselines/latest.json missing)."], True
    base = json.loads(BASELINE.read_text())["summary"]["by_split"].get("dev", {})
    now = summary["by_split"].get("dev", {})
    lines, ok = [], True
    for metric in GATE_METRICS:
        b, n = base.get(metric), now.get(metric)
        dropped = b is not None and (n is None or n < b)
        ok = ok and not dropped
        lines.append(f"{metric}: baseline {pct(b)} -> now {pct(n)} {'REGRESSION' if dropped else 'ok'}")
    return lines, ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score system outputs against answer key v2")
    ap.add_argument("--outputs", required=True, type=Path)
    ap.add_argument("--format", choices=("legacy", "contract"), required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--key-dir", type=Path, help="answer key of an external dataset (default: the 15 training samples)")
    ap.add_argument("--ignore-pages", action="store_true",
                    help="diagnostic: pool all pages before matching, to compare models despite the old pipeline's page-number bug")
    ap.add_argument("--save-baseline", action="store_true")
    ap.add_argument("--compare", action="store_true")
    args = ap.parse_args(argv)

    if args.key_dir:
        key_dir, plan = args.key_dir, [(p.stem, "external") for p in sorted(args.key_dir.glob("*.json"))]
    else:
        key_dir, plan = KEY_DIR, [(f"train_sample_{n}", split) for n, split in sorted(load_splits().items())]
    docs = []
    for doc_id, split in plan:
        key = from_contract(json.loads((key_dir / f"{doc_id}.json").read_text()))
        system = load_system(args.outputs / f"{doc_id}.json", doc_id, args.format)
        system.document_id = doc_id
        if args.ignore_pages:
            for ln in key.lines + system.lines:
                ln.page_no = None
        docs.append(as_dict(score_document(key, system, split, legacy=args.format == "legacy")))

    from .metrics import DocScore  # local import keeps the module surface small
    summary = summarise([DocScore(**d) for d in docs])
    gate, gate_ok = regression_gate(summary) if args.compare else (None, True)

    reports = EVAL / "reports"
    reports.mkdir(exist_ok=True)
    stem = reports / f"{date.today().isoformat()}_{args.label}"
    report = {"label": args.label, "date": date.today().isoformat(), "format": args.format,
              "outputs": str(args.outputs), "summary": summary, "documents": docs}
    stem.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    stem.with_suffix(".md").write_text(render_markdown(args.label, args.outputs, summary, docs, gate))
    print(f"Report: {stem}.md")
    for key, name in METRIC_ROWS:
        print(f"  {name:<48} " + "   ".join(f"{sp} {pct(agg.get(key)):>7}" for sp, agg in summary["by_split"].items()))
    if gate:
        print("\n".join("  gate: " + g for g in gate))
    if args.save_baseline and args.ignore_pages:
        print("Not saving a baseline from an --ignore-pages run.")
    elif args.save_baseline:
        BASELINE.parent.mkdir(exist_ok=True)
        # Numbers only: the examples quote bill lines, which stay out of git.
        slim = {**report, "documents": [{k: v for k, v in d.items() if k != "examples"} for d in docs]}
        BASELINE.write_text(json.dumps(slim, indent=2) + "\n")
        print(f"Saved baseline: {BASELINE}")
    return 0 if gate_ok else 2


if __name__ == "__main__":
    sys.exit(main())
