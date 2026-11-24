---
description: Demo on the fictional sample papers, scored against the hand-checked answer key
---

Run the workflow in CLAUDE.md offline, on the sample data:

1. Skip the PubMed search. Read `src/paper_metrics/sample_data/papers.json` (seven candidates; two are decoys to skip).
2. Extract every estimate into `output/extractions.json`. Do not open `src/paper_metrics/sample_data/gold.json`
   until step 5 - it is the answer key.
3. Validate with `python tools/validate.py output/extractions.json --papers src/paper_metrics/sample_data/papers.json`
   and fix any errors.
4. Compare with `python tools/compare.py output/extractions.json --papers src/paper_metrics/sample_data/papers.json --out output/comparison.md`.
5. Score with `python tools/score.py output/extractions.json --gold src/paper_metrics/sample_data/gold.json`.

Finish with the precision, recall and field accuracy, any warnings, and any estimate you missed or added.
