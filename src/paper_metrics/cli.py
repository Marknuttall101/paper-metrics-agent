"""Command line: paper-metrics <command>.

  paper-metrics demo                      offline demo of the whole agent loop (no API key, no AI)
  paper-metrics demo --live               real agents on the sample papers, scored against the answer key
  paper-metrics run --author "Smith JA"   real agents on an author's PubMed papers
  paper-metrics run --papers my.json      real agents on your own papers file
  paper-metrics validate | compare | score | pubmed ...   the individual tools
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from datetime import datetime
from importlib import resources
from pathlib import Path

from . import __version__
from ._console import utf8_output
from . import compare as compare_mod
from . import pubmed as pubmed_mod
from . import score as score_mod
from . import validate as validate_mod

TOOL_COMMANDS = {"validate": validate_mod.main, "compare": compare_mod.main,
                 "score": score_mod.main, "pubmed": pubmed_mod.main}


def sample_file(name: str) -> Path:
    """Path to a bundled sample file: 'papers.json' or 'gold.json'."""
    return Path(str(resources.files("paper_metrics") / "sample_data" / name))


def _load(path: str | Path) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(data, list):
        raise ValueError(f"{path} should contain a JSON list")
    return data


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def _positive_float(text: str) -> float:
    value = float(text)
    if not (math.isfinite(value) and value > 0):
        raise argparse.ArgumentTypeError("must be a number above 0")
    return value


# Environment variables that send Claude Code's calls somewhere other than the Anthropic API.
REDIRECTING_ENV = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_BASE_URL")


def _default_out() -> Path:
    return Path("output") / datetime.now().strftime("run-%Y%m%d-%H%M%S")


def _add_run_options(p: argparse.ArgumentParser, default_budget: float = 2.0) -> None:
    g = p.add_argument_group("limits")
    g.add_argument("--max-rounds", type=_positive_int, default=3, help="evaluator rounds before giving up (default 3)")
    g.add_argument("--max-budget-usd", type=_positive_float, default=default_budget,
                   help=f"spending cap for the whole run in US$ (default {default_budget:.2f})")
    g.add_argument("--no-sdk-budget-cap", dest="sdk_budget_cap", action="store_false",
                   help="enforce the budget in the pipeline only, without a per-call SDK cap: the agents "
                        "see no budget line, but a call can overshoot its share (see the README)")
    g.add_argument("--concurrency", type=_positive_int, default=4, help="workers running at once (default 4)")
    m = p.add_argument_group("models (aliases such as sonnet/opus/haiku, or full model IDs)")
    m.add_argument("--orchestrator-model", default="sonnet")
    m.add_argument("--worker-model", default="haiku")
    m.add_argument("--evaluator-model", default="sonnet")
    p.add_argument("--out", type=Path, help="output folder (default output/run-<timestamp>)")
    p.add_argument("--title", help="title for comparison.md")
    p.add_argument("--allow-other-auth", action="store_true",
                   help="run without ANTHROPIC_API_KEY (for Bedrock, Vertex AI or other providers Claude Code supports)")
    p.add_argument("--quiet", action="store_true", help="only print the final summary")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="paper-metrics", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"paper-metrics {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the agents on PubMed papers or your own papers file")
    src = run.add_mutually_exclusive_group(required=True)
    src.add_argument("--author", help='PubMed author, e.g. "Smith JA"')
    src.add_argument("--papers", help="a JSON list of {pmid, title, year, journal, authors, abstract}")
    run.add_argument("--term", help="extra PubMed search terms, e.g. a topic")
    run.add_argument("--max", type=_positive_int, default=10, help="papers to fetch from PubMed (default 10)")
    run.add_argument("--gold", help="answer key to score the result against")
    _add_run_options(run)

    demo = sub.add_parser("demo", help="the fictional sample papers (seven candidates, two of them decoys)")
    demo.add_argument("--live", action="store_true", help="use real agents (needs ANTHROPIC_API_KEY)")
    _add_run_options(demo, default_budget=0.5)

    for name in TOOL_COMMANDS:
        sub.add_parser(name, help=f"the {name} tool (see: paper-metrics {name} --help)", add_help=False)
    return ap


def _config(args, task: str, gold: list[dict] | None, title: str | None):
    from .agents import RoleSettings, RunConfig
    return RunConfig(
        task=task, out_dir=args.out or _default_out(), max_rounds=args.max_rounds,
        max_budget_usd=args.max_budget_usd, sdk_budget_cap=args.sdk_budget_cap, concurrency=args.concurrency,
        orchestrator=RoleSettings(args.orchestrator_model, 8),
        worker=RoleSettings(args.worker_model, 10),
        evaluator=RoleSettings(args.evaluator_model, 6),
        gold=gold, title=args.title or title)


def _print_outcome(outcome) -> None:
    from .agents.pipeline import STOP_MESSAGES
    print()
    print(STOP_MESSAGES[outcome.stop_reason])
    print(f"{len(outcome.records)} estimates, {outcome.rounds} round(s), cost US${outcome.cost_usd:.4f}")
    if outcome.score:
        s = outcome.score
        print(f"Against the answer key: precision {s['precision']:.0%}, recall {s['recall']:.0%}")
    print(f"Results: {outcome.out_dir / 'comparison.md'}  (full trace: run_report.json, run_log.md)")


def main(argv: list[str] | None = None) -> int:
    utf8_output()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in TOOL_COMMANDS:
        return TOOL_COMMANDS[argv[0]](argv[1:])
    args = build_parser().parse_args(argv)

    from .agents import AuthError, ClaudeSDKBackend, Pipeline, ScriptedBackend, check_api_key
    from .papers import normalise_papers
    from .pubmed import PubMedError, search_and_fetch

    log = (lambda _msg: None) if args.quiet else print
    try:
        if args.command == "demo":
            papers, gold = _load(sample_file("papers.json")), _load(sample_file("gold.json"))
            task = ("Extract every reported effect estimate from the sample papers on examplozumab (by L Novak and colleagues) "
                    "and compare them.")
            title = "Examplozumab across the sample papers (fictional data)"
            if args.live:
                check_api_key(args.allow_other_auth)
                backend = ClaudeSDKBackend()
            else:
                backend = ScriptedBackend(gold)
                if not args.quiet:
                    print("Offline demo: scripted agents, no AI is called and nothing is billed.\n")
            cfg = _config(args, task, gold, title)
        else:
            check_api_key(args.allow_other_auth)
            if args.author:
                print(f"Searching PubMed for {args.author!r}...")
                papers = search_and_fetch(args.author, args.term, args.max)
                task = f"Extract and compare the statistical results reported in papers by {args.author}"
                task += f" on {args.term}." if args.term else "."
            else:
                papers = _load(args.papers)
                task = f"Extract and compare the statistical results reported in the papers in {args.papers}."
            gold = _load(args.gold) if args.gold else None
            backend = ClaudeSDKBackend()
            cfg = _config(args, task, gold, None)
        papers = normalise_papers(papers)
        if isinstance(backend, ClaudeSDKBackend):
            redirected = [v for v in REDIRECTING_ENV if os.environ.get(v)]
            if redirected:
                print(f"Note: {', '.join(redirected)} is set, so calls may not go to (or be billed by) "
                      "the Anthropic API. run_report.json records the auth source.", file=sys.stderr)
        outcome = asyncio.run(Pipeline(backend, cfg, log=log).run(papers))
    except (AuthError, PubMedError, ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        return 130
    _print_outcome(outcome)
    return 0 if outcome.approved else 3


if __name__ == "__main__":
    sys.exit(main())
