"""The whole orchestrator -> workers -> evaluator loop, run offline with scripted and fake backends."""

import asyncio
import json

from paper_metrics.agents import AgentResult, Pipeline, RunConfig, ScriptedBackend
from paper_metrics.cli import sample_file

PAPERS = json.loads(sample_file("papers.json").read_text(encoding="utf-8"))
GOLD = json.loads(sample_file("gold.json").read_text(encoding="utf-8"))


def run_pipeline(backend, tmp_path, **kw):
    cfg = RunConfig(task="test", out_dir=tmp_path / "out", gold=GOLD, **kw)
    return asyncio.run(Pipeline(backend, cfg, log=lambda _m: None).run(PAPERS))


def test_scripted_demo_catches_the_mistake_and_is_approved_in_round_two(tmp_path):
    out = run_pipeline(ScriptedBackend(GOLD), tmp_path)
    assert out.approved and out.stop_reason == "approved" and out.rounds == 2
    assert out.score["precision"] == 1.0 and out.score["recall"] == 1.0
    report = json.loads((tmp_path / "out" / "run_report.json").read_text(encoding="utf-8"))
    assert report["rounds"][0]["evaluator"]["flagged"] == ["SAMPLE-002"]
    assert report["rounds"][0]["replan"]["redo"].keys() == {"SAMPLE-002"}
    assert [w["pmid"] for w in report["rounds"][1]["workers"]] == ["SAMPLE-002"]
    for name in ("papers.json", "extractions.json", "comparison.md", "evaluation.json", "run_log.md"):
        assert (tmp_path / "out" / name).exists()
    recs = json.loads((tmp_path / "out" / "extractions.json").read_text(encoding="utf-8"))
    assert all(r.get("outcome_group") for r in recs)
    assert "**Status:** Approved by the evaluator." in (tmp_path / "out" / "comparison.md").read_text(encoding="utf-8")


class NeverApproves(ScriptedBackend):
    """The evaluator always flags SAMPLE-002, so the run must stop at the round limit."""

    async def _evaluate(self, call, tools, result):
        pmids = [b.split('"', 1)[0] for b in call.prompt.split('<paper pmid="')[1:]]
        papers = [{"pmid": p, "status": "redo" if p == "SAMPLE-002" else "ok",
                   "instructions": "try again" if p == "SAMPLE-002" else ""} for p in pmids]
        await self._use(tools["submit_verdict"], {"done": False, "papers": papers}, result)


def test_round_limit_stops_the_run_without_approval(tmp_path):
    out = run_pipeline(NeverApproves(GOLD), tmp_path, max_rounds=2)
    assert not out.approved and out.stop_reason == "max_rounds" and out.rounds == 2
    assert "NOT approved" in (tmp_path / "out" / "comparison.md").read_text(encoding="utf-8")


class Silent:
    """A model that never calls its submit tool."""
    name = "silent"

    def __init__(self):
        self.calls = []

    async def run(self, call):
        self.calls.append(call.label)
        return AgentResult(label=call.label, role=call.role, cost_usd=0.01)


def test_a_missing_submit_is_retried_once_then_the_run_stops(tmp_path):
    backend = Silent()
    out = run_pipeline(backend, tmp_path)
    assert out.stop_reason == "plan_failed" and not out.records
    assert backend.calls == ["orchestrator: plan", "orchestrator: plan (retry)"]


class SpendsItsBudget(ScriptedBackend):
    """Every call spends its whole per-call budget (as a real call can), and workers never submit."""

    async def run(self, call):
        if call.role == "worker":
            result = AgentResult(label=call.label, role="worker")
        else:
            result = await super().run(call)
        result.cost_usd = call.max_budget_usd
        return result


def test_budget_is_a_hard_cap(tmp_path):
    out = run_pipeline(SpendsItsBudget(GOLD), tmp_path, max_budget_usd=1.0)
    assert not out.approved
    assert out.cost_usd <= 1.0 + 1e-9
    report = json.loads((tmp_path / "out" / "run_report.json").read_text(encoding="utf-8"))
    assert report["rounds"], "the round that ran must be in the report"


class RecordsCaps(ScriptedBackend):
    """Records the per-call cap each call is given, and charges a fixed cost per call."""

    def __init__(self, gold, cost):
        super().__init__(gold)
        self.caps, self.cost = [], cost

    async def run(self, call):
        self.caps.append(call.max_budget_usd)
        result = await super().run(call)
        result.cost_usd = self.cost
        return result


def test_sdk_budget_cap_is_passed_by_default(tmp_path):
    backend = RecordsCaps(GOLD, cost=0.001)
    run_pipeline(backend, tmp_path)
    assert backend.caps and all(c is not None and c > 0 for c in backend.caps)


def test_without_the_sdk_cap_no_call_gets_a_cap_but_the_pipeline_still_tracks_spend(tmp_path):
    backend = RecordsCaps(GOLD, cost=0.001)
    out = run_pipeline(backend, tmp_path, sdk_budget_cap=False)
    assert out.approved and backend.caps and all(c is None for c in backend.caps)
    assert abs(out.cost_usd - 0.001 * len(backend.caps)) < 1e-9
    report = json.loads((tmp_path / "out" / "run_report.json").read_text(encoding="utf-8"))
    assert report["limits"]["sdk_budget_cap"] is False


def test_without_the_sdk_cap_the_pipeline_still_stops_when_the_budget_is_spent(tmp_path):
    backend = RecordsCaps(GOLD, cost=0.3)          # every call costs far more than its share
    out = run_pipeline(backend, tmp_path, max_budget_usd=1.0, sdk_budget_cap=False)
    assert out.stop_reason == "budget" and not out.approved
    assert all(c is None for c in backend.caps)


class FailsRedo(NeverApproves):
    """Round 1 succeeds; the SAMPLE-002 redo in round 2 never submits."""

    async def _worker(self, call, tools, result):
        if call.label.startswith("worker SAMPLE-002 round 2"):
            return
        await super()._worker(call, tools, result)


def test_a_failed_redo_is_reported_to_the_evaluator(tmp_path):
    backend = FailsRedo(GOLD)
    seen = {}
    original = backend._evaluate

    async def spy(call, tools, result):
        seen[call.label] = call.prompt
        await original(call, tools, result)
    backend._evaluate = spy
    run_pipeline(backend, tmp_path, max_rounds=2)
    assert "WORKER FAILED" in seen["evaluator round 2"]
    assert "earlier round" in seen["evaluator round 2"]


class DropsEverything(NeverApproves):
    async def _evaluate(self, call, tools, result):
        pmids = [b.split('"', 1)[0] for b in call.prompt.split('<paper pmid="')[1:]]
        await self._use(tools["submit_verdict"], {"done": False, "papers": [
            {"pmid": p, "status": "redo", "instructions": "x"} for p in pmids]}, result)

    async def _replan(self, call, tools, result):
        pmids = [b.split('"', 1)[0] for b in call.prompt.split('<paper pmid="')[1:]]
        await self._use(tools["submit_round_plan"], {"papers": [
            {"pmid": p, "decision": "drop", "reason": "no"} for p in pmids]}, result)


def test_dropping_every_paper_is_not_an_approval(tmp_path):
    out = run_pipeline(DropsEverything(GOLD), tmp_path)
    assert out.stop_reason == "all_dropped" and not out.approved and out.records == []


class DropsFlagged(NeverApproves):
    """The orchestrator drops SAMPLE-002 instead of redoing it; the rest is then approved."""

    async def _evaluate(self, call, tools, result):
        if '<paper pmid="SAMPLE-002">' in call.prompt:
            return await super()._evaluate(call, tools, result)
        return await ScriptedBackend._evaluate(self, call, tools, result)

    async def _replan(self, call, tools, result):
        await self._use(tools["submit_round_plan"],
                        {"papers": [{"pmid": "SAMPLE-002", "decision": "drop", "reason": "not extractable"}]}, result)


def test_dropped_papers_leave_the_results(tmp_path):
    out = run_pipeline(DropsFlagged(GOLD), tmp_path)
    assert out.approved
    assert "SAMPLE-002" not in {r["pmid"] for r in out.records}
    report = json.loads((tmp_path / "out" / "run_report.json").read_text(encoding="utf-8"))
    assert report["papers"]["dropped"] == {"SAMPLE-002": "not extractable"}


class RunsOutOfTurns:
    """The plan call runs out of turns the first time, as a live agent might."""
    name = "turns"

    def __init__(self):
        self.calls = []

    async def run(self, call):
        self.calls.append(call.label)
        return AgentResult(label=call.label, role=call.role, cost_usd=0.01, stop_subtype="error_max_turns",
                           error="agent stopped: Reached maximum number of turns (8)")


def test_running_out_of_turns_is_retried(tmp_path):
    backend = RunsOutOfTurns()
    run_pipeline(backend, tmp_path)
    assert backend.calls == ["orchestrator: plan", "orchestrator: plan (retry)"]


class CrashesWithoutCost:
    name = "crash"

    async def run(self, call):
        return AgentResult(label=call.label, role=call.role, cost_known=False, error="ProcessError: boom")


def test_a_call_with_unknown_cost_is_charged_its_reservation(tmp_path):
    out = run_pipeline(CrashesWithoutCost(), tmp_path, max_budget_usd=1.0)
    assert out.stop_reason == "plan_failed"
    assert 0 < out.cost_usd <= 1.0  # charged, not free, and no retry after a non-retryable error


def test_budget_must_be_a_finite_positive_number(tmp_path):
    import math
    import pytest
    for bad in (0, -1, math.nan, math.inf):
        with pytest.raises(ValueError):
            RunConfig(task="t", out_dir=tmp_path, max_budget_usd=bad)
