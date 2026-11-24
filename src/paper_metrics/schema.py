"""The extraction record format, shared by every part of the project."""

from __future__ import annotations

METRICS = (
    "hazard_ratio", "odds_ratio", "risk_ratio", "mean_difference",
    "standardised_mean_difference", "correlation", "auc", "sensitivity",
    "specificity", "accuracy", "proportion", "other",
)
RATIO_METRICS = frozenset({"hazard_ratio", "odds_ratio", "risk_ratio"})
REQUIRED_FIELDS = ("pmid", "metric", "value", "outcome", "evidence")

METRIC_LABELS = {
    "hazard_ratio": "Hazard ratio", "odds_ratio": "Odds ratio", "risk_ratio": "Risk ratio",
    "mean_difference": "Mean difference", "standardised_mean_difference": "Standardised mean difference",
    "correlation": "Correlation", "auc": "AUC", "sensitivity": "Sensitivity",
    "specificity": "Specificity", "accuracy": "Accuracy", "proportion": "Proportion", "other": "Other",
}

_NUM_OR_NULL = {"type": ["number", "null"]}
_STR_OR_NULL = {"type": ["string", "null"]}

# JSON Schema for one record as a worker submits it. The pipeline adds pmid, id and round.
RECORD_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "metric": {"type": "string", "enum": list(METRICS)},
        "value": {"type": "number", "description": "The estimate exactly as written (88% -> 88)."},
        "ci_low": {**_NUM_OR_NULL, "description": "Lower interval bound, or null if not reported."},
        "ci_high": {**_NUM_OR_NULL, "description": "Upper interval bound, or null if not reported."},
        "ci_level": {**_NUM_OR_NULL, "description": "Interval level, e.g. 95 or 90. Null if no interval."},
        "p_value": {**_STR_OR_NULL, "description": "As written, e.g. \"0.002\" or \"<0.001\". Null if not reported."},
        "n": {"type": ["integer", "null"], "description": "Number analysed for this estimate, or null."},
        "unit": {**_STR_OR_NULL, "description": "e.g. \"%\" or \" m\". Null for ratios."},
        "outcome": {"type": "string", "description": "What was measured, in a few words."},
        "comparison": {**_STR_OR_NULL, "description": "e.g. \"drug vs placebo\". Null for single-arm metrics."},
        "evidence": {"type": "string", "description": "Word-for-word quote from the abstract containing the numbers."},
    },
    "required": ["metric", "value", "outcome", "evidence"],
}


def record_id(pmid: str, index: int) -> str:
    """Stable id for the index-th record of a paper, e.g. 'SAMPLE-001#2'."""
    return f"{pmid}#{index}"
