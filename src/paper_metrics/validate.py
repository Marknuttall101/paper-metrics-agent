"""Check extraction records against the source abstracts.

This is the step that stops made-up numbers. For every record it checks:

ERRORS (must be fixed)
  - required fields are present, numbers are numbers, and the metric is one of the allowed names
  - the paper (pmid) exists in the papers file
  - the evidence quote (at least three words) appears word for word in that paper's abstract,
    starting and ending on whole numbers - a quote of "0.81" cut from "0.815" is rejected
  - value, ci_low and ci_high are written in the evidence quote with the same sign,
    and so is the p-value
  - ci_low <= value <= ci_high, and both bounds are given or neither
WARNINGS (a human should look)
  - the interval is not written just after the value in the quote (it may belong to another estimate)
  - ci_level is not written in the quote
  - n is not written as digits anywhere in the abstract (e.g. "Sixty-two patients")
  - a ratio metric (HR, OR, RR) with a value <= 0

What it cannot check: that an estimate is paired with the right outcome, that n belongs to the
estimate rather than being some other number in the abstract, or scientific-notation p-values
beyond their first digits. That is the evaluator's job (or yours).

Usage
  paper-metrics validate output/extractions.json --papers output/papers.json
Exit code 0 = no errors, 1 = errors found.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from ._console import utf8_output
from .numparse import (SIGN, collapse_whitespace, is_sign_position, normalise_text, number_appears, numbers_in,
                       p_value_number)
from .schema import METRICS, RATIO_METRICS, REQUIRED_FIELDS


MIN_QUOTE_WORDS = 3


def _norm(s: str) -> str:
    return collapse_whitespace(normalise_text(s)).lower()


def _cuts_a_number(text: str, start: int, end: int) -> bool:
    """True if text[start:end] begins or ends in the middle of a number."""
    before = text[start - 1] if start > 0 else " "
    after = text[end] if end < len(text) else " "
    after2 = text[end + 1] if end + 1 < len(text) else " "
    first, last = text[start], text[end - 1]
    signed_before = before == SIGN or (before == "-" and is_sign_position(text, start - 1))
    cut_start = (first.isdigit() or first == ".") and (before.isdigit() or before == "." or signed_before)
    cut_end = last.isdigit() and (after.isdigit() or (after == "." and after2.isdigit()))
    return cut_start or cut_end


def quote_in_text(quote: str, text: str) -> bool:
    """True if the quote appears in the text (ignoring case, spacing and number typography)
    without starting or ending in the middle of a number."""
    q, t = _norm(quote), _norm(text)
    if not q:
        return False
    for m in re.finditer(re.escape(q), t):
        if not _cuts_a_number(t, m.start(), m.end()):
            return True
    return False


def _interval_follows_value(rec: dict, evidence: str) -> bool:
    """True if ci_low then ci_high appear shortly after the value in the quote."""
    nums = numbers_in(evidence)

    def at(x) -> list[int]:
        return [i for i, n in enumerate(nums) if abs(n - float(x)) <= 1e-9]

    for i in at(rec["value"]):
        for j in at(rec["ci_low"]):
            for k in at(rec["ci_high"]):
                if i < j < k and j - i <= 3 and k - j <= 2:
                    return True
    return False


def _is_number(x) -> bool:
    if isinstance(x, bool):
        return False
    try:
        float(x)
        return True
    except (TypeError, ValueError):
        return False


def check_record(rec: dict, paper: dict | None) -> tuple[list[str], list[str]]:
    """Errors and warnings for one record (messages without a prefix)."""
    errors: list[str] = []
    warnings: list[str] = []
    missing = [k for k in REQUIRED_FIELDS if rec.get(k) in (None, "")]
    if missing:
        return [f"missing required field(s) {', '.join(missing)}"], []
    if rec["metric"] not in METRICS:
        errors.append(f"metric '{rec['metric']}' is not one of {', '.join(METRICS)}")
    if paper is None:
        return errors + ["pmid not found in the papers file"], warnings
    bad = [f for f in ("value", "ci_low", "ci_high", "ci_level", "n") if rec.get(f) is not None and not _is_number(rec[f])]
    if bad:
        return errors + [f"{f}={rec[f]!r} is not a number" for f in bad], warnings
    val = float(rec["value"])
    evidence = str(rec["evidence"])
    abstract = paper.get("abstract") or ""
    if len(evidence.split()) < MIN_QUOTE_WORDS:
        errors.append(f"evidence quote is too short - quote the whole clause (at least {MIN_QUOTE_WORDS} words)")
    elif not quote_in_text(evidence, abstract):
        errors.append("evidence quote is not word-for-word in the abstract")
    for field in ("value", "ci_low", "ci_high"):
        if rec.get(field) is not None and not number_appears(rec[field], evidence):
            errors.append(f"{field}={rec[field]} is not written in the evidence quote (check the sign too)")
    p_num = p_value_number(rec.get("p_value"))
    if p_num is not None and not number_appears(p_num, evidence, signed=False):
        errors.append(f"p_value={rec['p_value']} is not written in the evidence quote")
    lo, hi = rec.get("ci_low"), rec.get("ci_high")
    if (lo is None) != (hi is None):
        errors.append("give both ci_low and ci_high, or neither")
    elif lo is not None:
        if not (float(lo) <= val <= float(hi)):
            errors.append(f"value {rec['value']} is outside its interval [{lo}, {hi}]")
        elif not errors and not _interval_follows_value(rec, evidence):
            warnings.append("the interval is not written right after the value in the quote - "
                            "check it belongs to this estimate")
    if rec.get("ci_level") is not None and not number_appears(rec["ci_level"], evidence):
        warnings.append(f"ci_level={rec['ci_level']} is not written in the quote - check it")
    if rec.get("n") is not None and not number_appears(rec["n"], abstract):
        warnings.append(f"n={rec['n']} is not written as digits in the abstract - check it by hand")
    if rec["metric"] in RATIO_METRICS and val <= 0:
        warnings.append(f"a {rec['metric']} should be above 0")
    return errors, warnings


def validate(extractions: list[dict], papers: list[dict]) -> tuple[list[str], list[str]]:
    """Errors and warnings for a list of records, each prefixed with the record it is about."""
    by_pmid = {str(p["pmid"]): p for p in papers}
    errors: list[str] = []
    warnings: list[str] = []
    for i, rec in enumerate(extractions):
        tag = f"#{i + 1} ({rec.get('id') or rec.get('pmid', '?')} {rec.get('metric', '?')})"
        e, w = check_record(rec, by_pmid.get(str(rec.get("pmid"))))
        errors += [f"{tag}: {m}" for m in e]
        warnings += [f"{tag}: {m}" for m in w]
    return errors, warnings


def main(argv: list[str] | None = None) -> int:
    utf8_output()
    ap = argparse.ArgumentParser(prog="paper-metrics validate", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("extractions")
    ap.add_argument("--papers", required=True)
    args = ap.parse_args(argv)
    extractions = json.loads(Path(args.extractions).read_text(encoding="utf-8"))
    papers = json.loads(Path(args.papers).read_text(encoding="utf-8"))
    errors, warnings = validate(extractions, papers)
    for e in errors:
        print("ERROR  ", e)
    for w in warnings:
        print("WARNING", w)
    print(f"\n{len(extractions)} records checked: {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
