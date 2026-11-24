"""The tools each agent role gets: they must accept good input and reject bad input with a fixable message."""

import asyncio
import copy
import json

from paper_metrics.agents.roles import (RoleSettings, VerdictState, evaluator_call, plan_call, round_plan_call,
                                        synthesis_call, worker_call)
from paper_metrics.cli import sample_file

PAPERS = json.loads(sample_file("papers.json").read_text(encoding="utf-8"))
GOLD = json.loads(sample_file("gold.json").read_text(encoding="utf-8"))
BY_PMID = {p["pmid"]: p for p in PAPERS}
S = RoleSettings("test-model", 5)


def run(coro):
    return asyncio.run(coro)


def tool(call, name):
    return next(t for t in call.tools if t.name == name)


def gold_for(pmid):
    return [{k: v for k, v in r.items() if k not in ("pmid", "note")} for r in GOLD if r["pmid"] == pmid]


# ---------------------------------------------------------------- plan

def test_plan_accepts_a_decision_for_every_paper():
    call, state = plan_call("task", PAPERS, S, 1.0)
    entries = [{"pmid": p["pmid"], "decision": "extract", "instructions": "x"} for p in PAPERS[:-1]]
    entries.append({"pmid": PAPERS[-1]["pmid"], "decision": "exclude", "reason": "editorial"})
    reply = run(tool(call, "submit_plan").handler({"papers": entries}))
    assert not reply.is_error
    assert state.accepted and len(state.selected) == len(PAPERS) - 1
    assert state.excluded == {PAPERS[-1]["pmid"]: "editorial"}


def test_plan_rejects_missing_unknown_and_reasonless_entries():
    call, state = plan_call("task", PAPERS, S, 1.0)
    reply = run(tool(call, "submit_plan").handler({"papers": [
        {"pmid": "SAMPLE-001", "decision": "extract"},
        {"pmid": "NOPE", "decision": "extract"},
        {"pmid": "SAMPLE-002", "decision": "exclude"},
    ]}))
    assert reply.is_error and not state.accepted
    assert "unknown pmid 'NOPE'" in reply.text
    assert "give a reason" in reply.text
    assert "no decision for:" in reply.text and "SAMPLE-003" in reply.text


def test_plan_refuses_to_extract_a_paper_without_an_abstract():
    papers = copy.deepcopy(PAPERS[:1]) + [{"pmid": "X1", "title": "Letter", "abstract": ""}]
    call, state = plan_call("task", papers, S, 1.0)
    reply = run(tool(call, "submit_plan").handler({"papers": [
        {"pmid": "SAMPLE-001", "decision": "extract"}, {"pmid": "X1", "decision": "extract"}]}))
    assert reply.is_error and "X1 has no abstract" in reply.text


def test_plan_accepts_json_string_arrays():
    call, state = plan_call("task", PAPERS[:1], S, 1.0)
    reply = run(tool(call, "submit_plan").handler({"papers": json.dumps([{"pmid": "SAMPLE-001", "decision": "extract"}])}))
    assert not reply.is_error and state.accepted


# ---------------------------------------------------------------- worker

def test_worker_accepts_correct_records_and_injects_pmid():
    call, state = worker_call(BY_PMID["SAMPLE-001"], "", 1, S, 1.0)
    reply = run(tool(call, "submit_records").handler({"records": gold_for("SAMPLE-001")}))
    assert not reply.is_error and state.accepted
    assert {r["pmid"] for r in state.records} == {"SAMPLE-001"}


def test_worker_rejects_a_number_that_is_not_in_the_quote():
    call, state = worker_call(BY_PMID["SAMPLE-001"], "", 1, S, 1.0)
    recs = gold_for("SAMPLE-001")
    recs[0]["value"] = 0.72
    check = run(tool(call, "check_records").handler({"records": recs}))
    assert "value=0.72 is not written in the evidence quote" in check.text
    submit = run(tool(call, "submit_records").handler({"records": recs}))
    assert submit.is_error and not state.accepted


def test_worker_rejects_duplicates():
    call, state = worker_call(BY_PMID["SAMPLE-001"], "", 1, S, 1.0)
    recs = gold_for("SAMPLE-001")[:1] * 2
    reply = run(tool(call, "submit_records").handler({"records": recs}))
    assert reply.is_error and "duplicate" in reply.text


def test_worker_empty_submission_needs_notes():
    call, state = worker_call(BY_PMID["SAMPLE-001"], "", 1, S, 1.0)
    assert run(tool(call, "submit_records").handler({"records": []})).is_error
    assert not run(tool(call, "submit_records").handler({"records": [], "notes": "no results reported"})).is_error


def test_worker_normalises_p_values():
    call, state = worker_call(BY_PMID["SAMPLE-001"], "", 1, S, 1.0)
    recs = gold_for("SAMPLE-001")
    recs[0]["p_value"] = "P=0.002"
    run(tool(call, "submit_records").handler({"records": recs}))
    assert state.records[0]["p_value"] == "0.002"


def test_worker_prompt_carries_feedback_and_previous_attempt_on_a_redo():
    prev = [dict(r, pmid="SAMPLE-002", id="SAMPLE-002#1") for r in gold_for("SAMPLE-002")[:1]]
    call, _ = worker_call(BY_PMID["SAMPLE-002"], "watch for AUC", 2, S, 1.0, previous=prev, feedback="AUC missing")
    assert "AUC missing" in call.prompt and "[SAMPLE-002#1]" in call.prompt and "watch for AUC" in call.prompt


# ---------------------------------------------------------------- evaluator

def _eval(validation=None, failed=frozenset()):
    papers = [BY_PMID["SAMPLE-001"], BY_PMID["SAMPLE-002"]]
    return evaluator_call("task", papers, {}, {}, set(failed), validation or {}, 1, 3, S, 1.0)


def test_evaluator_done_requires_every_paper_ok():
    call, state = _eval()
    reply = run(tool(call, "submit_verdict").handler({"done": True, "papers": [
        {"pmid": "SAMPLE-001", "status": "ok"},
        {"pmid": "SAMPLE-002", "status": "redo", "instructions": "add AUC"}]}))
    assert reply.is_error and "done=true but some papers" in reply.text


def test_evaluator_cannot_pass_a_paper_with_validator_errors():
    call, state = _eval(validation={"SAMPLE-002": ["value wrong"]})
    reply = run(tool(call, "submit_verdict").handler({"done": True, "papers": [
        {"pmid": "SAMPLE-001", "status": "ok"}, {"pmid": "SAMPLE-002", "status": "ok"}]}))
    assert reply.is_error and "cannot be 'ok': SAMPLE-002" in reply.text


def test_evaluator_must_say_done_when_everything_is_ok():
    call, state = _eval()
    reply = run(tool(call, "submit_verdict").handler({"done": False, "papers": [
        {"pmid": "SAMPLE-001", "status": "ok"}, {"pmid": "SAMPLE-002", "status": "ok"}]}))
    assert reply.is_error and "set done=true" in reply.text


def test_evaluator_redo_needs_instructions_and_is_recorded():
    call, state = _eval()
    h = tool(call, "submit_verdict").handler
    assert run(h({"done": False, "papers": [{"pmid": "SAMPLE-001", "status": "ok"},
                                            {"pmid": "SAMPLE-002", "status": "redo"}]})).is_error
    reply = run(h({"done": False, "quality_score": 70, "papers": [
        {"pmid": "SAMPLE-001", "status": "ok"},
        {"pmid": "SAMPLE-002", "status": "redo", "issues": "AUC missing", "instructions": "add the AUC"}]}))
    assert not reply.is_error and list(state.flagged()) == ["SAMPLE-002"]
    assert state.papers["SAMPLE-002"]["issues"] == ["AUC missing"]


# ---------------------------------------------------------------- re-plan and synthesis

def test_round_plan_covers_exactly_the_flagged_papers():
    verdict = VerdictState(accepted=True, papers={
        "SAMPLE-001": {"status": "ok", "issues": [], "instructions": ""},
        "SAMPLE-002": {"status": "redo", "issues": ["AUC missing"], "instructions": "add AUC"}})
    call, state = round_plan_call("task", verdict, PAPERS, {}, S, 1.0)
    h = tool(call, "submit_round_plan").handler
    assert run(h({"papers": [{"pmid": "SAMPLE-001", "decision": "redo", "instructions": "x"}]})).is_error
    assert not run(h({"papers": [{"pmid": "SAMPLE-002", "decision": "redo", "instructions": "add AUC"}]})).is_error
    assert state.redo == {"SAMPLE-002": "add AUC"}


def test_synthesis_needs_a_group_for_every_record():
    records = [dict(r, id=f"{r['pmid']}#{i}") for i, r in enumerate(GOLD[:2], 1)]
    call, state = synthesis_call("task", PAPERS, records, S, 1.0)
    h = tool(call, "submit_synthesis").handler
    assert run(h({"outcome_groups": [{"record_id": records[0]["id"], "group": "A"}], "summary": "s"})).is_error
    reply = run(h({"outcome_groups": [{"record_id": r["id"], "group": "A"} for r in records], "summary": "s"}))
    assert not reply.is_error and state.groups == {r["id"]: "A" for r in records}


def test_evaluator_cannot_approve_an_empty_set():
    call, state = evaluator_call("task", [], {}, {}, set(), {}, 1, 3, S, 1.0)
    reply = run(tool(call, "submit_verdict").handler({"done": True, "papers": []}))
    assert reply.is_error and "no papers to review" in reply.text


def test_evaluator_reads_done_strictly():
    call, state = _eval()
    reply = run(tool(call, "submit_verdict").handler({"done": "false", "papers": [
        {"pmid": "SAMPLE-001", "status": "ok"},
        {"pmid": "SAMPLE-002", "status": "redo", "instructions": "x"}]}))
    assert not reply.is_error and state.done is False


def test_round_plan_rejects_a_paper_listed_twice():
    verdict = VerdictState(accepted=True, papers={
        "SAMPLE-002": {"status": "redo", "issues": [], "instructions": "x"}})
    call, state = round_plan_call("task", verdict, PAPERS, {}, S, 1.0)
    reply = run(tool(call, "submit_round_plan").handler({"papers": [
        {"pmid": "SAMPLE-002", "decision": "redo", "instructions": "x"},
        {"pmid": "SAMPLE-002", "decision": "drop", "reason": "y"}]}))
    assert reply.is_error and "listed twice" in reply.text
