"""paper-metrics-agent: extract and compare statistical results across research papers.

Two ways to run it:
  * a multi-agent pipeline (orchestrator, workers, evaluator) on the Claude Agent SDK,
    paid for with an Anthropic API key - see ``paper_metrics.agents``;
  * Claude Code itself as the agent, on your own Claude subscription - see CLAUDE.md.

The modules at this level (pubmed, validate, compare, score) never call a model.
"""

__version__ = "0.2.0"
