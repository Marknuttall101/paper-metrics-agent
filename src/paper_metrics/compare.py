"""Compare extracted metrics across papers and write a Markdown report.

Records are grouped by metric and, when the records carry an ``outcome_group``
(the multi-agent pipeline's orchestrator assigns one), by outcome too, so
like is compared with like. Each group lists every paper's estimate,
interval, n and p-value. For ratio metrics it says how many estimates sit
below or above 1 and how many intervals exclude 1.

Usage
  paper-metrics compare output/extractions.json --papers output/papers.json --out output/comparison.md
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from .schema import METRIC_LABELS, METRICS, RATIO_METRICS
from .validate import validate
from ._console import utf8_output


def _decimals(x: float) -> int:
    text = f"{float(x):g}"
    return len(text.split(".")[1]) if "." in text and "e" not in text else 0


def format_estimate(rec: dict) -> str:
    """Value and interval with a shared number of decimals, e.g. '0.74 (95% CI 0.61 to 0.90)'."""
    nums = [float(rec[k]) for k in ("value", "ci_low", "ci_high") if rec.get(k) is not None]
    d = max(_decimals(x) for x in nums)

    def f(x) -> str:
        return f"{float(x):.{d}f}"

    est = f"{f(rec['value'])}{rec.get('unit') or ''}"
    if rec.get("ci_low") is not None and rec.get("ci_high") is not None:
        try:
            level = f"{float(rec.get('ci_level') or 95):g}%"
        except (TypeError, ValueError):
            level = str(rec.get("ci_level"))
        est += f" ({level} CI {f(rec['ci_low'])} to {f(rec['ci_high'])})"
    return est


def direction_summary(recs: list[dict]) -> str:
    below = sum(float(r["value"]) < 1 for r in recs)
    above = sum(float(r["value"]) > 1 for r in recs)
    with_ci = [r for r in recs if r.get("ci_low") is not None and r.get("ci_high") is not None]
    excl = sum(1 for r in with_ci if float(r["ci_high"]) < 1 or float(r["ci_low"]) > 1)
    text = (f"{below} of {len(recs)} estimates below 1, {above} above 1; "
            f"intervals excluding 1: {excl} of {len(with_ci)}")
    if len({(r.get("outcome_group") or r["outcome"]).strip().lower() for r in recs}) > 1:
        text += " (these are different outcomes - compare like with like)"
    return text


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def build_report(extractions: list[dict], papers: list[dict], title: str | None = None,
                 summary: str | None = None) -> str:
    meta = {str(p["pmid"]): p for p in papers}
    grouped = any(r.get("outcome_group") for r in extractions)
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for rec in extractions:
        key = (rec["metric"], (rec.get("outcome_group") or "").strip() if grouped else "")
        groups[key].append(rec)

    errors, warnings = validate(extractions, papers)
    n_papers = len({str(r["pmid"]) for r in extractions})
    lines = [f"# {title or 'Metric comparison across papers'}", ""]
    if summary:
        lines += [summary.strip(), ""]
    lines.append(f"{len(extractions)} estimates from {n_papers} papers. Checked against the abstracts "
                 f"by the validator: {len(errors)} error(s), {len(warnings)} warning(s).")
    lines.append("")
    for msg in errors:
        lines.append(f"- ERROR: {msg}")
    for msg in warnings:
        lines.append(f"- Warning: {msg}")
    if errors or warnings:
        lines.append("")

    def sort_key(k):  # ratios first, in the schema's metric order, then by outcome group
        return (METRICS.index(k[0]) if k[0] in METRICS else len(METRICS), k[1].lower())

    for key in sorted(groups, key=sort_key):
        metric, outcome_group = key
        recs = sorted(groups[key], key=lambda r: (str(meta.get(str(r["pmid"]), {}).get("year") or ""), str(r["pmid"])))
        heading = METRIC_LABELS.get(metric, metric)
        if outcome_group:
            heading += f" - {outcome_group}"
        lines += [f"## {heading} ({len(recs)})", ""]
        lines += ["| Paper | Year | Outcome | Comparison | Estimate | n | p |",
                  "|---|---|---|---|---|---|---|"]
        for r in recs:
            p = meta.get(str(r["pmid"]), {})
            n = r.get("n") if r.get("n") is not None else "-"
            lines.append(f"| {_cell(r['pmid'])} | {_cell(p.get('year', '-'))} | {_cell(r['outcome'])} "
                         f"| {_cell(r.get('comparison') or '-')} | {_cell(format_estimate(r))} | {n} "
                         f"| {_cell(r.get('p_value') or '-')} |")
        if metric in RATIO_METRICS and len(recs) > 1:
            lines += ["", f"Direction: {direction_summary(recs)}."]
        lines.append("")
    lines += ["## Papers", ""]
    for pmid in sorted({str(r["pmid"]) for r in extractions}):
        p = meta.get(pmid, {})
        lines.append(f"- {pmid}: {p.get('title', '(title not found)')} ({p.get('journal', '')} {p.get('year', '')})")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    utf8_output()
    ap = argparse.ArgumentParser(prog="paper-metrics compare", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("extractions")
    ap.add_argument("--papers", required=True)
    ap.add_argument("--out")
    ap.add_argument("--title")
    args = ap.parse_args(argv)
    extractions = json.loads(Path(args.extractions).read_text(encoding="utf-8"))
    papers = json.loads(Path(args.papers).read_text(encoding="utf-8"))
    report = build_report(extractions, papers, args.title)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"Saved comparison to {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
