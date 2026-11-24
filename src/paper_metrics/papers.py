"""Loading and tidying a papers file: a JSON list of {pmid, title, year, journal, authors, abstract}."""

from __future__ import annotations

import json
from pathlib import Path


def normalise_papers(raw: object) -> list[dict]:
    """Check and tidy a list of papers. Raises ValueError with a clear message on bad input.

    pmid is required and must be unique (any id works, not just PubMed ids); every other
    field is optional. Text fields become strings and authors a list of strings.
    """
    if not isinstance(raw, list):
        raise ValueError("the papers file should contain a JSON list of papers")
    papers, seen = [], set()
    for i, p in enumerate(raw, 1):
        if not isinstance(p, dict):
            raise ValueError(f"paper {i} is not an object")
        pmid = "" if p.get("pmid") is None else str(p["pmid"]).strip()
        if not pmid:
            raise ValueError(f"paper {i} has no 'pmid' (any unique id will do)")
        if pmid in seen:
            raise ValueError(f"pmid {pmid!r} appears more than once")
        seen.add(pmid)
        authors = p.get("authors") or []
        if isinstance(authors, str):
            authors = [a.strip() for a in authors.split(",") if a.strip()]
        papers.append({
            **p,
            "pmid": pmid,
            "title": str(p.get("title") or ""),
            "journal": str(p.get("journal") or ""),
            "year": str(p.get("year") or ""),
            "authors": [str(a) for a in authors],
            "abstract": str(p.get("abstract") or ""),
        })
    return papers


def load_papers(path: str | Path) -> list[dict]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} is not valid JSON: {e}") from e
    return normalise_papers(raw)
