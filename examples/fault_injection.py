"""Does the evaluator really catch mistakes? A live check of the redo loop.

Runs the real agents on the sample papers, except that the round-1 worker for
SAMPLE-002 is swapped for a scripted one that leaves out the AUC the abstract
reports. A working team should: flag SAMPLE-002 (evaluator), turn the issue
into instructions (orchestrator), fix it in round 2 (worker), then approve.

Needs ANTHROPIC_API_KEY. Costs roughly US$0.15 with the default models.
  python examples/fault_injection.py --out output/fault-check
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from paper_metrics.agents import ClaudeSDKBackend, Pipeline, RunConfig, ScriptedBackend, check_api_key
from paper_metrics.cli import sample_file


class FaultInjecting:
    name = "claude-agent-sdk + one injected fault"

    def __init__(self, gold: list[dict]):
        self.real = ClaudeSDKBackend()
        self.scripted = ScriptedBackend(gold)

    async def run(self, call):
        if call.label == "worker SAMPLE-002 round 1":
            return await self.scripted.run(call)   # submits the odds ratio but not the AUC
        return await self.real.run(call)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path("output/fault-check"))
    ap.add_argument("--max-budget-usd", type=float, default=1.0)
    ap.add_argument("--no-sdk-budget-cap", dest="sdk_budget_cap", action="store_false",
                    help="track the budget in the pipeline only (see the README)")
    ap.add_argument("--allow-other-auth", action="store_true")
    args = ap.parse_args()
    check_api_key(args.allow_other_auth)
    papers = json.loads(sample_file("papers.json").read_text(encoding="utf-8"))
    gold = json.loads(sample_file("gold.json").read_text(encoding="utf-8"))
    cfg = RunConfig(task="Extract every reported effect estimate from the sample papers on examplozumab "
                         "(by L Novak and colleagues) and compare them.",
                    out_dir=args.out, max_budget_usd=args.max_budget_usd, sdk_budget_cap=args.sdk_budget_cap,
                    gold=gold, title="Fault-injection check")
    out = asyncio.run(Pipeline(FaultInjecting(gold), cfg).run(papers))
    verdicts = json.loads((args.out / "evaluation.json").read_text(encoding="utf-8"))
    flagged = verdicts[0]["papers"].get("SAMPLE-002", {}) if verdicts else {}
    print(f"\nEvaluator on the injected fault: {flagged.get('status', 'no verdict')} - "
          f"{'; '.join(flagged.get('issues', [])) or '-'}")
    caught = flagged.get("status") == "redo" and out.approved and bool(out.score) and out.score["recall"] == 1.0
    print("PASS: the fault was caught and fixed." if caught else "FAIL: the fault was not caught and fixed.")
    return 0 if caught else 1


if __name__ == "__main__":
    sys.exit(main())
