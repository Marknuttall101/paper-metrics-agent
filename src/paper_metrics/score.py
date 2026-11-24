"""Score extractions against a hand-checked answer key.

A record counts as found when the pmid, metric and value match a gold record.
For found records it also checks the interval, n and p-value.

Usage
  paper-metrics score output/extractions.json --gold sample_data/gold.json
  paper-metrics score output/extractions.json --gold sample_data/gold.json --min-recall 0.9
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ._console import utf8_output

TOL = 1e-6


def _key(r: dict):
    return (str(r["pmid"]), r["metric"], round(float(r["value"]), 6))


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) <= TOL
    except (TypeError, ValueError):
        return str(a).replace(" ", "").lower() == str(b).replace(" ", "").lower()


def score(extractions: list[dict], gold: list[dict]) -> dict:
    """Precision, recall and field accuracy. Matching is one-to-one, so two records with the
    same paper, metric and value (e.g. one HR for two outcomes) are counted separately."""
    unmatched_gold: dict[tuple, list[dict]] = {}
    for g in gold:
        unmatched_gold.setdefault(_key(g), []).append(g)
    pairs, extra = [], []
    for p in extractions:
        bucket = unmatched_gold.get(_key(p))
        if bucket:
            pairs.append((bucket.pop(0), p))
        else:
            extra.append(_key(p))
    missed = [k for k, rest in unmatched_gold.items() for _ in rest]
    field_checks = {"ci": 0, "n": 0, "p_value": 0}
    for g, p in pairs:
        field_checks["ci"] += _same(g.get("ci_low"), p.get("ci_low")) and _same(g.get("ci_high"), p.get("ci_high"))
        field_checks["n"] += _same(g.get("n"), p.get("n"))
        field_checks["p_value"] += _same(g.get("p_value"), p.get("p_value"))
    n_found = len(pairs)
    return {
        "precision": n_found / len(extractions) if extractions else 0.0,
        "recall": n_found / len(gold) if gold else 0.0,
        "found": n_found, "missed": sorted(missed), "extra": sorted(extra),
        "field_accuracy": {f: (c / n_found if n_found else 0.0) for f, c in field_checks.items()},
    }


def main(argv: list[str] | None = None) -> int:
    utf8_output()
    ap = argparse.ArgumentParser(prog="paper-metrics score", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("extractions")
    ap.add_argument("--gold", required=True)
    ap.add_argument("--min-recall", type=float, default=None)
    args = ap.parse_args(argv)
    pred = json.loads(Path(args.extractions).read_text(encoding="utf-8"))
    gold = json.loads(Path(args.gold).read_text(encoding="utf-8"))
    s = score(pred, gold)
    print(f"Precision {s['precision']:.0%}  Recall {s['recall']:.0%}  ({s['found']} of {len(gold)} gold estimates found)")
    print("Field accuracy on found estimates: " + ", ".join(f"{k} {v:.0%}" for k, v in s["field_accuracy"].items()))
    for k in s["missed"]:
        print("MISSED ", k)
    for k in s["extra"]:
        print("EXTRA  ", k, "(not in the answer key - check whether it is real)")
    if args.min_recall is not None and s["recall"] < args.min_recall:
        print(f"Recall below {args.min_recall:.0%}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
