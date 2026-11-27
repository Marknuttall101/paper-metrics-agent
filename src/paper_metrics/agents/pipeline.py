"""The orchestrator -> workers -> evaluator loop.

    plan (orchestrator)
      -> round 1: one worker per paper, in parallel
      -> code checks every number against its quote
      -> evaluator: done? if not, which papers need redoing and why
      -> re-plan (orchestrator): turn the evaluator's issues into worker instructions
      -> round 2: only the flagged papers ... and so on
    until the evaluator approves, the round limit is reached or the budget runs out
      -> synthesis (orchestrator): outcome groups + summary
      -> files: run_report.json, run_log.md, extractions.json, comparison.md, evaluation.json

Budget: before every agent call the pipeline reserves that call's maximum spend out of what is
left, and passes it to the SDK as the call's own cap. Reservations are released when the call
ends and replaced by what it actually cost. Workers leave a share of the budget for the
evaluator, re-plan and synthesis steps. With ``sdk_budget_cap=False`` the reservation is not passed
to the SDK (so the CLI adds no budget line to the agents' context and enforces no per-call cap).
Reservations and accounting work the same, but a call is stopped only by its turn limit and
timeout, so a run can overshoot ``max_budget_usd`` by what its last calls spend.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..compare import build_report
from ..papers import normalise_papers
from ..schema import record_id
from ..score import score as score_records
from ..validate import validate
from .backend import AgentCall, AgentResult, Backend
from .roles import (RoleSettings, evaluator_call, plan_call, round_plan_call, synthesis_call,
                    worker_call)

MIN_CALL_BUDGET = 0.005          # US$: below this an agent call is not worth starting
WORKER_HOLDBACK = 0.25           # share of the total budget kept back from workers for review steps
REVIEW_HOLDBACK = 0.10           # share kept back from the evaluator and re-plan for the synthesis


@dataclass
class RunConfig:
    task: str
    out_dir: Path
    max_rounds: int = 3
    max_budget_usd: float = 2.0
    sdk_budget_cap: bool = True      # also pass each call's reservation to the backend as its own cap
    concurrency: int = 4
    call_timeout_s: float = 600.0
    orchestrator: RoleSettings = field(default_factory=lambda: RoleSettings("sonnet", 8))
    worker: RoleSettings = field(default_factory=lambda: RoleSettings("haiku", 10))
    evaluator: RoleSettings = field(default_factory=lambda: RoleSettings("sonnet", 6))
    gold: list[dict] | None = None
    title: str | None = None

    def __post_init__(self):
        if self.max_rounds < 1 or self.concurrency < 1:
            raise ValueError("max_rounds and concurrency must be at least 1")
        if not (math.isfinite(self.max_budget_usd) and self.max_budget_usd > 0):
            raise ValueError("max_budget_usd must be a number above 0")


@dataclass
class RunOutcome:
    stop_reason: str
    approved: bool
    records: list[dict]
    cost_usd: float
    rounds: int
    out_dir: Path
    score: dict | None = None


class BudgetExhausted(Exception):
    pass


class _Stop(Exception):
    """Ends the loop early with a stop reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


STOP_MESSAGES = {
    "approved": "Approved by the evaluator.",
    "max_rounds": "NOT approved: the round limit was reached before the evaluator signed off.",
    "budget": "NOT approved: the budget ran out before the evaluator signed off.",
    "nothing_to_extract": "No papers were in scope, so there was nothing to extract.",
    "all_dropped": "NOT approved: every paper in scope was dropped as not extractable.",
    "plan_failed": "Stopped: the orchestrator did not produce a valid plan.",
    "evaluator_failed": "NOT approved: the evaluator did not produce a valid verdict.",
    "error": "NOT approved: the run stopped on an unexpected error (see run_log.md).",
}

# Errors where a second attempt is worth it: the agent ran out of turns or simply never submitted.
_RETRYABLE = ("max_turns", "maximum number of turns")


class Pipeline:
    def __init__(self, backend: Backend, config: RunConfig, log: Callable[[str], None] = print):
        self.backend = backend
        self.cfg = config
        self._print = log
        self.log_lines: list[str] = []
        self.results: list[AgentResult] = []
        self.spent = 0.0
        self.committed = 0.0
        self.started = time.monotonic()

    # ------------------------------------------------------------------ bookkeeping
    def log(self, msg: str) -> None:
        line = f"[{time.monotonic() - self.started:6.1f}s] {msg}"
        self.log_lines.append(line)
        self._print(line)

    def _reserve(self, holdback_share: float = 0.0, split: int = 1) -> float:
        """Reserve a budget for one agent call, or raise BudgetExhausted."""
        free = self.cfg.max_budget_usd * (1 - holdback_share) - self.spent - self.committed
        amount = free / max(split, 1)
        if amount < MIN_CALL_BUDGET:
            raise BudgetExhausted()
        self.committed += amount
        return amount

    async def _call(self, call: AgentCall, budget: float) -> AgentResult:
        call = dataclasses.replace(call, max_budget_usd=budget if self.cfg.sdk_budget_cap else None)
        try:
            result = await asyncio.wait_for(self.backend.run(call), timeout=self.cfg.call_timeout_s)
        except asyncio.TimeoutError:
            result = AgentResult(label=call.label, role=call.role, model=call.model, cost_known=False,
                                 error=f"timed out after {self.cfg.call_timeout_s:.0f}s")
        finally:
            self.committed -= budget
        if not result.cost_known:
            # The call ended without reporting its cost (timeout, crash): assume it spent its whole
            # reservation, so the cap still holds.
            result.cost_usd = max(result.cost_usd, budget)
        self.results.append(result)
        self.spent += result.cost_usd
        status = f"error: {result.error}" if result.error else "ok"
        self.log(f"{result.label}: {status} | model {result.model} | {result.turns} turns "
                 f"| US${result.cost_usd:.4f} | tools: {', '.join(result.tool_calls) or 'none'}")
        return result

    async def _run(self, call: AgentCall, state: Any, tool: str, holdback_share: float = 0.0,
                   split: int = 1) -> AgentResult:
        """Run an agent call. If it ends without a successful submit, try once more in a fresh session."""
        result = await self._call(call, self._reserve(holdback_share, split))
        retryable = (result.error is None or result.stop_subtype == "error_max_turns"
                     or any(k in result.error.lower() for k in _RETRYABLE))
        if not state.accepted and retryable:
            nudge = (f"\n\nNote: a previous attempt at this task ended without a successful call to {tool}. "
                     f"Finish by calling {tool}; if it returns an error, fix what it says and call it again.")
            retry = dataclasses.replace(call, label=call.label + " (retry)", prompt=call.prompt + nudge)
            result = await self._call(retry, self._reserve(holdback_share, split))
        return result

    # ------------------------------------------------------------------ main loop
    async def run(self, papers: list[dict]) -> RunOutcome:
        cfg = self.cfg
        papers = normalise_papers(papers)
        cfg.out_dir.mkdir(parents=True, exist_ok=True)
        by_pmid = {p["pmid"]: p for p in papers}
        self.log(f"Task: {cfg.task} | {len(papers)} candidate papers | backend {self.backend.name} "
                 f"| max {cfg.max_rounds} rounds, US${cfg.max_budget_usd:.2f}"
                 f"{'' if cfg.sdk_budget_cap else ' (pipeline only, no SDK cap)'}")

        records: dict[str, list[dict]] = {}
        notes: dict[str, str] = {}
        instructions: dict[str, str] = {}
        excluded: dict[str, str] = {}
        dropped: dict[str, str] = {}
        rounds_log: list[dict] = []
        verdicts: list[dict] = []
        plan_notes = ""
        stop_reason = "max_rounds"
        round_no = 0
        last_verdict = None

        try:
            # 1. plan
            call, plan = plan_call(cfg.task, papers, cfg.orchestrator, None)
            await self._run(call, plan, "submit_plan", REVIEW_HOLDBACK)
            if not plan.accepted:
                raise _Stop("plan_failed")
            instructions, excluded, plan_notes = dict(plan.selected), dict(plan.excluded), plan.notes
            self.log(f"Plan: extract {len(instructions)}, exclude {len(excluded)}"
                     + "".join(f"\n    excluded {p}: {r}" for p, r in excluded.items()))
            if not instructions:
                raise _Stop("nothing_to_extract")

            to_run = list(instructions)
            feedback: dict[str, str] = {}
            for round_no in range(1, cfg.max_rounds + 1):
                entry: dict[str, Any] = {"round": round_no, "workers": []}
                rounds_log.append(entry)

                # 2. workers, in parallel; each reserves its share of what is left when it starts
                failed_now: set[str] = set()
                if to_run:
                    self.log(f"Round {round_no}: {len(to_run)} worker(s)")
                    sem = asyncio.Semaphore(cfg.concurrency)
                    pending = {"n": len(to_run)}

                    async def one(pmid: str, rn: int = round_no):
                        async with sem:
                            call, st = worker_call(by_pmid[pmid], instructions[pmid], rn, cfg.worker, None,
                                                   previous=records.get(pmid), feedback=feedback.get(pmid))
                            share, pending["n"] = pending["n"], pending["n"] - 1
                            try:
                                res = await self._run(call, st, "submit_records", WORKER_HOLDBACK, share)
                            except BudgetExhausted:
                                res = AgentResult(label=call.label, role="worker", error="skipped: budget exhausted")
                                self.log(f"{call.label}: skipped - budget exhausted")
                            return pmid, st, res

                    for pmid, st, res in await asyncio.gather(*(one(p) for p in to_run)):
                        if st.accepted:
                            records[pmid] = [dict(r, id=record_id(pmid, i), round=round_no)
                                             for i, r in enumerate(st.records, 1)]
                            notes[pmid] = st.notes
                        else:
                            failed_now.add(pmid)
                        entry["workers"].append({"pmid": pmid, "accepted": st.accepted, "records": len(st.records),
                                                 "checks": st.checks, "warnings": st.warnings,
                                                 "cost_usd": res.cost_usd, "error": res.error})
                active = [p for p in instructions if p not in dropped]
                failed = {p for p in active if p not in records} | failed_now

                # 3. deterministic checks
                validation: dict[str, list[str]] = {}
                for p in active:
                    errs, _ = validate(records.get(p, []), papers)
                    if errs:
                        validation[p] = errs
                n_records = sum(len(records.get(p, [])) for p in active)
                self.log(f"Validator: {sum(len(v) for v in validation.values())} error(s) across {n_records} "
                         f"records; {len(failed)} paper(s) without an accepted submission this round")

                # 4. evaluator
                call, verdict = evaluator_call(cfg.task, [by_pmid[p] for p in active], records, notes, failed,
                                               validation, round_no, cfg.max_rounds, cfg.evaluator, None)
                await self._run(call, verdict, "submit_verdict", REVIEW_HOLDBACK)
                if not verdict.accepted:
                    raise _Stop("evaluator_failed")
                last_verdict = verdict
                verdicts.append({"round": round_no, "done": verdict.done, "quality_score": verdict.quality_score,
                                 "summary": verdict.summary, "papers": verdict.papers})
                entry["evaluator"] = {"done": verdict.done, "quality_score": verdict.quality_score,
                                      "flagged": sorted(verdict.flagged())}
                self.log(f"Evaluator: done={verdict.done}, score={verdict.quality_score}, "
                         f"flagged {len(verdict.flagged())}: {', '.join(sorted(verdict.flagged())) or '-'}")
                if verdict.done:
                    raise _Stop("approved")
                if round_no == cfg.max_rounds:
                    raise _Stop("max_rounds")

                # 5. re-plan
                call, rp = round_plan_call(cfg.task, verdict, papers, records, cfg.orchestrator, None)
                await self._run(call, rp, "submit_round_plan", REVIEW_HOLDBACK)
                if rp.accepted:
                    redo, drop = rp.redo, rp.drop
                else:
                    self.log("Re-plan failed; passing the evaluator's instructions straight to the workers")
                    redo, drop = {p: v["instructions"] for p, v in verdict.flagged().items()}, {}
                for p, reason in drop.items():
                    dropped[p] = reason
                    records.pop(p, None)
                feedback = {p: self._feedback_text(verdict.papers[p], redo[p]) for p in redo}
                to_run = list(redo)
                entry["replan"] = {"redo": redo, "drop": drop}
                self.log(f"Re-plan: redo {len(redo)}, drop {len(drop)}")
                if not [p for p in instructions if p not in dropped]:
                    raise _Stop("all_dropped")
        except _Stop as s:
            stop_reason = s.reason
        except BudgetExhausted:
            stop_reason = "budget"
            self.log("Budget exhausted - stopping")
        except Exception as e:  # keep the trace of a paid run even if something unexpected breaks
            stop_reason = "error"
            self.log(f"Unexpected error: {type(e).__name__}: {e}\n{traceback.format_exc()}")

        # 6. synthesis and outputs
        active = [p for p in instructions if p not in dropped]
        final = [r for p in active for r in records.get(p, [])]
        summary = None
        if final and stop_reason != "error":
            try:
                call, syn = synthesis_call(cfg.task, papers, final, cfg.orchestrator, None)
                await self._run(call, syn, "submit_synthesis")
                if syn.accepted:
                    for r in final:
                        r["outcome_group"] = syn.groups.get(r["id"])
                    summary = syn.summary
            except BudgetExhausted:
                self.log("No budget left for the synthesis step; the report has no outcome groups")
        approved = stop_reason == "approved"
        score = score_records(final, cfg.gold) if cfg.gold is not None else None
        if score:
            self.log(f"Score against the answer key: precision {score['precision']:.0%}, recall {score['recall']:.0%}")
        self.log(f"Finished: {STOP_MESSAGES[stop_reason]} Total cost US${self.spent:.4f}")
        self._write(papers, final, excluded, dropped, plan_notes, verdicts, rounds_log, stop_reason,
                    summary, score, last_verdict)
        return RunOutcome(stop_reason, approved, final, self.spent, round_no, cfg.out_dir, score)

    @staticmethod
    def _feedback_text(paper_verdict: dict, orchestrator_instructions: str) -> str:
        issues = "\n".join(f"- {i}" for i in paper_verdict.get("issues", []))
        return (f"Evaluator issues:\n{issues or '- (none listed)'}\n"
                f"Evaluator instructions: {paper_verdict.get('instructions') or '-'}\n"
                f"Orchestrator instructions for this attempt: {orchestrator_instructions}")

    # ------------------------------------------------------------------ outputs
    def _write(self, papers, final, excluded, dropped, plan_notes, verdicts, rounds_log, stop_reason,
               summary, score, last_verdict) -> None:
        out = self.cfg.out_dir
        status = STOP_MESSAGES[stop_reason]
        if last_verdict and last_verdict.quality_score is not None:
            status += f" Final evaluator score: {last_verdict.quality_score}/100."
        by_role: dict[str, float] = {}
        for r in self.results:
            by_role[r.role] = by_role.get(r.role, 0.0) + r.cost_usd
        report = {
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "task": self.cfg.task,
            "backend": self.backend.name,
            "stop_reason": stop_reason,
            "status": status,
            "approved": stop_reason == "approved",
            "limits": {"max_rounds": self.cfg.max_rounds, "max_budget_usd": self.cfg.max_budget_usd,
                       "sdk_budget_cap": self.cfg.sdk_budget_cap, "concurrency": self.cfg.concurrency},
            "models": {"orchestrator": self.cfg.orchestrator.model, "worker": self.cfg.worker.model,
                       "evaluator": self.cfg.evaluator.model},
            "papers": {"candidates": len(papers), "excluded": excluded, "dropped": dropped,
                       "extracted": len({r["pmid"] for r in final})},
            "plan_notes": plan_notes,
            "records": len(final),
            "rounds": rounds_log,
            "cost_usd": {"total": round(self.spent, 6), "by_role": {k: round(v, 6) for k, v in by_role.items()}},
            "api_key_sources": sorted({r.api_key_source for r in self.results if r.api_key_source}),
            "agent_calls": [dataclasses.asdict(r) for r in self.results],
            "score": score,
        }
        # The trace first: it is the record of what was spent, so it must survive a later failure.
        writes = [
            ("run_report.json", lambda: json.dumps(report, indent=2, ensure_ascii=False) + "\n"),
            ("run_log.md", lambda: "# Run log\n\n```\n" + "\n".join(self.log_lines) + "\n```\n"),
            ("extractions.json", lambda: json.dumps(final, indent=2, ensure_ascii=False) + "\n"),
            ("evaluation.json", lambda: json.dumps(verdicts, indent=2, ensure_ascii=False) + "\n"),
            ("papers.json", lambda: json.dumps(papers, indent=2, ensure_ascii=False) + "\n"),
            ("comparison.md", lambda: build_report(final, papers, self.cfg.title,
                                                   f"**Status:** {status}" + (f"\n\n{summary}" if summary else ""))),
        ]
        for name, render in writes:
            try:
                (out / name).write_text(render(), encoding="utf-8")
            except Exception as e:
                self.log(f"Could not write {name}: {type(e).__name__}: {e}")
