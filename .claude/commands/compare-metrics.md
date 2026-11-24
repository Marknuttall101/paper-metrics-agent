---
description: Find an author's PubMed papers, extract their statistical results, verify every number and compare across papers
argument-hint: '"Lastname Initials" [max papers] [topic]'
---

Run the full workflow in CLAUDE.md for these arguments: $ARGUMENTS

- The first argument is the PubMed author name (e.g. "Smith JA"). The second, if given, is the
  maximum number of papers (default 10). Anything after that is a topic to add with `--term`.
- Save papers to `output/papers.json`, extractions to `output/extractions.json` and the report to
  `output/comparison.md`.
- Do not finish until `tools/validate.py` reports 0 errors.
- End with a short summary: papers found, estimates extracted, warnings to check by hand, and what
  the comparison shows. Point out when estimates are for different outcomes and should not be compared directly.
