"""The three roles: what each agent is told, which tools it gets, and how its tool calls are checked.

Each ``*_call`` function returns an ``AgentCall`` plus a small state object.
The agent's tool calls fill that state; the pipeline reads it afterwards.
Every submit tool checks its input and answers with an error the agent can
fix, so bad output is caught while the agent is still running.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..compare import format_estimate
from ..numparse import clean_p_value
from ..schema import RECORD_INPUT_SCHEMA
from ..validate import check_record
from . import prompts
from .backend import AgentCall, ToolReply, ToolSpec

RECORD_FIELDS = tuple(RECORD_INPUT_SCHEMA["properties"])


@dataclass
class RoleSettings:
    model: str
    max_turns: int


# --------------------------------------------------------------------------- helpers

def paper_block(paper: dict, max_authors: int = 8) -> str:
    authors = paper.get("authors") or []
    shown = ", ".join(authors[:max_authors]) + (" et al." if len(authors) > max_authors else "")
    abstract = paper.get("abstract") or "(no abstract)"
    return (f'<paper pmid="{paper["pmid"]}">\n'
            f"<title>{paper.get('title', '')}</title>\n"
            f"<journal>{paper.get('journal', '')} {paper.get('year', '')}</journal>\n"
            f"<authors>{shown}</authors>\n"
            f"<abstract>\n{abstract}\n</abstract>\n</paper>")


def record_line(rec: dict) -> str:
    parts = [f"[{rec['id']}]" if rec.get("id") else "", rec["metric"], format_estimate(rec),
             f"n={rec.get('n')}" if rec.get("n") is not None else "",
             f"p={rec['p_value']}" if rec.get("p_value") else "",
             f"| outcome: {rec['outcome']}", f"| comparison: {rec.get('comparison') or '-'}",
             f'| evidence: "{rec["evidence"]}"']
    return " ".join(p for p in parts if p)


def _clean_record(raw: Any, pmid: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("each record must be an object")
    rec = {k: raw.get(k) for k in RECORD_FIELDS}
    rec["p_value"] = clean_p_value(rec["p_value"])
    rec["pmid"] = pmid
    return rec


def _records_from_args(args: dict, pmid: str) -> list[dict]:
    records = args.get("records")
    if isinstance(records, str):  # some models send the array as a JSON string
        records = json.loads(records)
    if not isinstance(records, list):
        raise ValueError("'records' must be a list")
    return [_clean_record(r, pmid) for r in records]


def _list_arg(args: dict, key: str) -> list:
    value = args.get(key)
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError(f"'{key}' must be a list")
    return value


def _as_bool(x: Any) -> bool:
    if isinstance(x, str):
        return x.strip().lower() in ("true", "yes", "1")
    return bool(x)


def _check_all(records: list[dict], paper: dict) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    seen = set()
    for i, rec in enumerate(records, 1):
        e, w = check_record(rec, paper)
        key = (rec.get("metric"), rec.get("value"), rec.get("ci_low"), rec.get("ci_high"),
               (rec.get("outcome") or "").strip().lower())
        if key in seen:
            e = e + ["duplicate of an earlier record"]
        seen.add(key)
        errors += [f"record {i} ({rec.get('metric')} {rec.get('value')}): {m}" for m in e]
        warnings += [f"record {i} ({rec.get('metric')} {rec.get('value')}): {m}" for m in w]
    return errors, warnings


# --------------------------------------------------------------------------- orchestrator: plan

@dataclass
class PlanState:
    accepted: bool = False
    selected: dict[str, str] = field(default_factory=dict)   # pmid -> worker instructions
    excluded: dict[str, str] = field(default_factory=dict)   # pmid -> reason
    notes: str = ""


def plan_call(task: str, papers: list[dict], settings: RoleSettings, budget: float | None) -> tuple[AgentCall, PlanState]:
    state = PlanState()
    by_pmid = {str(p["pmid"]): p for p in papers}

    async def submit_plan(args: dict) -> ToolReply:
        entries = _list_arg(args, "papers")
        selected, excluded, problems = {}, {}, []
        for e in entries:
            pmid = str(e.get("pmid", ""))
            if pmid not in by_pmid:
                problems.append(f"unknown pmid {pmid!r}")
                continue
            if pmid in selected or pmid in excluded:
                problems.append(f"pmid {pmid} listed twice")
                continue
            if e.get("decision") == "extract":
                if not (by_pmid[pmid].get("abstract") or "").strip():
                    problems.append(f"pmid {pmid} has no abstract - exclude it")
                    continue
                selected[pmid] = (e.get("instructions") or "").strip()
            elif e.get("decision") == "exclude":
                if not (e.get("reason") or "").strip():
                    problems.append(f"pmid {pmid}: give a reason for excluding it")
                    continue
                excluded[pmid] = e["reason"].strip()
            else:
                problems.append(f"pmid {pmid}: decision must be 'extract' or 'exclude'")
        missing = [p for p in by_pmid if p not in selected and p not in excluded]
        if missing:
            problems.append(f"no decision for: {', '.join(missing)}")
        if problems:
            return ToolReply("Plan not accepted. Fix and call submit_plan again:\n- " + "\n- ".join(problems), True)
        state.selected, state.excluded = selected, excluded
        state.notes = (args.get("notes") or "").strip()
        state.accepted = True
        return ToolReply(f"Plan accepted: {len(selected)} papers to extract, {len(excluded)} excluded.")

    tool = ToolSpec(
        "submit_plan",
        "Submit the plan: a decision for every candidate paper, with worker instructions or an exclusion reason.",
        {"type": "object", "properties": {
            "papers": {"type": "array", "items": {"type": "object", "properties": {
                "pmid": {"type": "string"},
                "decision": {"type": "string", "enum": ["extract", "exclude"]},
                "instructions": {"type": "string", "description": "For 'extract': what the worker should watch for."},
                "reason": {"type": "string", "description": "For 'exclude': why."}},
                "required": ["pmid", "decision"]}},
            "notes": {"type": "string", "description": "One or two lines on the set as a whole."}},
         "required": ["papers"]},
        submit_plan)
    prompt = (f"Task: {task}\n\nThere are {len(papers)} candidate papers. Decide on every one, then call submit_plan.\n\n"
              + "\n\n".join(paper_block(p) for p in papers))
    call = AgentCall("orchestrator", "orchestrator: plan", prompts.ORCHESTRATOR, prompt, [tool],
                     settings.model, settings.max_turns, budget)
    return call, state


# --------------------------------------------------------------------------- worker

@dataclass
class WorkerState:
    accepted: bool = False
    records: list[dict] = field(default_factory=list)
    notes: str = ""
    warnings: list[str] = field(default_factory=list)
    checks: int = 0


def worker_call(paper: dict, instructions: str, round_no: int, settings: RoleSettings, budget: float | None,
                previous: list[dict] | None = None, feedback: str | None = None) -> tuple[AgentCall, WorkerState]:
    state = WorkerState()
    pmid = str(paper["pmid"])

    async def check_records(args: dict) -> ToolReply:
        state.checks += 1
        records = _records_from_args(args, pmid)
        errors, warnings = _check_all(records, paper)
        if not records:
            return ToolReply("No records given. If the abstract really has no estimates, submit an empty list with notes.")
        lines = [f"{len(records)} records checked: {len(errors)} error(s), {len(warnings)} warning(s)."]
        lines += [f"ERROR {m}" for m in errors] + [f"WARNING {m}" for m in warnings]
        if not errors:
            lines.append("No errors. Call submit_records when the list is complete.")
        return ToolReply("\n".join(lines))

    async def submit_records(args: dict) -> ToolReply:
        records = _records_from_args(args, pmid)
        notes = (args.get("notes") or "").strip()
        errors, warnings = _check_all(records, paper)
        if errors:
            return ToolReply("Not accepted - fix these and submit again:\n" + "\n".join(f"ERROR {m}" for m in errors), True)
        if not records and not notes:
            return ToolReply("An empty submission needs 'notes' explaining why the abstract has no estimates.", True)
        state.records, state.notes, state.warnings, state.accepted = records, notes, warnings, True
        extra = f" Warnings for a human to check: {len(warnings)}." if warnings else ""
        return ToolReply(f"Accepted {len(records)} records.{extra}")

    records_schema = {"type": "array", "items": RECORD_INPUT_SCHEMA}
    tools = [
        ToolSpec("check_records", "Check draft records against the abstract. Returns errors to fix.",
                 {"type": "object", "properties": {"records": records_schema}, "required": ["records"]}, check_records),
        ToolSpec("submit_records", "Submit the final records for this paper (rejected if any record has an error).",
                 {"type": "object", "properties": {
                     "records": records_schema,
                     "notes": {"type": "string", "description": "Anything the evaluator should know, e.g. why a number was left out."}},
                  "required": ["records"]}, submit_records),
    ]
    parts = [paper_block(paper), "", f"Orchestrator's instructions: {instructions or 'none'}"]
    if feedback:
        parts += ["", "This paper is being redone. The reviewers' feedback on the previous attempt:", feedback]
    if previous:
        parts += ["", "Previous submission:"] + [record_line(r) for r in previous]
    parts += ["", "Extract every estimate, check the records, fix any errors, then call submit_records."]
    call = AgentCall("worker", f"worker {pmid} round {round_no}", prompts.WORKER, "\n".join(parts), tools,
                     settings.model, settings.max_turns, budget)
    return call, state


# --------------------------------------------------------------------------- evaluator

@dataclass
class VerdictState:
    accepted: bool = False
    done: bool = False
    quality_score: int | None = None
    summary: str = ""
    papers: dict[str, dict] = field(default_factory=dict)   # pmid -> {"status", "issues", "instructions"}

    def flagged(self) -> dict[str, dict]:
        return {p: v for p, v in self.papers.items() if v["status"] == "redo"}


def evaluator_call(task: str, papers: list[dict], records_by_pmid: dict[str, list[dict]],
                   notes_by_pmid: dict[str, str], failed: set[str], validation: dict[str, list[str]],
                   round_no: int, max_rounds: int, settings: RoleSettings,
                   budget: float | None) -> tuple[AgentCall, VerdictState]:
    state = VerdictState()
    pmids = [str(p["pmid"]) for p in papers]

    async def submit_verdict(args: dict) -> ToolReply:
        entries = _list_arg(args, "papers")
        verdicts, problems = {}, []
        for e in entries:
            pmid = str(e.get("pmid", ""))
            status = e.get("status")
            if pmid not in pmids:
                problems.append(f"unknown pmid {pmid!r}")
            elif pmid in verdicts:
                problems.append(f"pmid {pmid} listed twice")
            elif status not in ("ok", "redo"):
                problems.append(f"pmid {pmid}: status must be 'ok' or 'redo'")
            elif status == "redo" and not (e.get("instructions") or "").strip():
                problems.append(f"pmid {pmid}: 'redo' needs instructions saying exactly what to fix")
            else:
                issues = e.get("issues") or []
                if isinstance(issues, str):
                    issues = [issues]
                verdicts[pmid] = {"status": status, "issues": [str(i) for i in issues],
                                  "instructions": (e.get("instructions") or "").strip()}
        missing = [p for p in pmids if p not in verdicts]
        if missing:
            problems.append(f"no verdict for: {', '.join(missing)}")
        must_redo = [p for p in pmids if (validation.get(p) or p in failed) and verdicts.get(p, {}).get("status") == "ok"]
        if must_redo:
            problems.append(f"these papers have validator errors or no accepted submission, so they cannot be 'ok': {', '.join(must_redo)}")
        done = _as_bool(args.get("done"))
        if not pmids:
            problems.append("there are no papers to review")
        if done and any(v["status"] == "redo" for v in verdicts.values()):
            problems.append("done=true but some papers are marked 'redo'")
        if not done and verdicts and all(v["status"] == "ok" for v in verdicts.values()) and not problems:
            problems.append("every paper is 'ok' - set done=true, or mark the papers that need work as 'redo'")
        if problems:
            return ToolReply("Verdict not accepted:\n- " + "\n- ".join(problems), True)
        score = args.get("quality_score")
        state.quality_score = int(score) if isinstance(score, (int, float)) else None
        state.done, state.papers, state.summary = done, verdicts, (args.get("summary") or "").strip()
        state.accepted = True
        return ToolReply("Verdict recorded.")

    tool = ToolSpec(
        "submit_verdict",
        "Submit the review: a status for every paper and whether the whole job is done.",
        {"type": "object", "properties": {
            "done": {"type": "boolean", "description": "True only when every paper is 'ok'."},
            "quality_score": {"type": "integer", "minimum": 0, "maximum": 100},
            "summary": {"type": "string", "description": "Two or three sentences on overall quality."},
            "papers": {"type": "array", "items": {"type": "object", "properties": {
                "pmid": {"type": "string"},
                "status": {"type": "string", "enum": ["ok", "redo"]},
                "issues": {"type": "array", "items": {"type": "string"}},
                "instructions": {"type": "string", "description": "For 'redo': exactly what the worker must fix."}},
                "required": ["pmid", "status"]}}},
         "required": ["done", "papers"]},
        submit_verdict)

    blocks = []
    for p in papers:
        pmid = str(p["pmid"])
        recs = records_by_pmid.get(pmid, [])
        lines = [paper_block(p), f"Submitted records ({len(recs)}):"]
        lines += [record_line(r) for r in recs] or ["(none)"]
        if pmid in failed:
            lines.append("WORKER FAILED: no accepted submission this round"
                         + (" - the records above are from an earlier round." if recs else "."))
        if notes_by_pmid.get(pmid):
            lines.append(f"Worker notes: {notes_by_pmid[pmid]}")
        lines += [f"VALIDATOR ERROR: {m}" for m in validation.get(pmid, [])]
        blocks.append("\n".join(lines))
    prompt = (f"Task: {task}\nReview round {round_no} of at most {max_rounds}.\n\n"
              + "\n\n".join(blocks) + "\n\nReview every paper, then call submit_verdict.")
    call = AgentCall("evaluator", f"evaluator round {round_no}", prompts.EVALUATOR, prompt, [tool],
                     settings.model, settings.max_turns, budget)
    return call, state


# --------------------------------------------------------------------------- orchestrator: re-plan

@dataclass
class RoundPlanState:
    accepted: bool = False
    redo: dict[str, str] = field(default_factory=dict)   # pmid -> worker instructions
    drop: dict[str, str] = field(default_factory=dict)   # pmid -> reason


def round_plan_call(task: str, verdict: VerdictState, papers: list[dict], records_by_pmid: dict[str, list[dict]],
                    settings: RoleSettings, budget: float | None) -> tuple[AgentCall, RoundPlanState]:
    state = RoundPlanState()
    flagged = verdict.flagged()

    async def submit_round_plan(args: dict) -> ToolReply:
        redo, drop, problems = {}, {}, []
        for e in _list_arg(args, "papers"):
            pmid = str(e.get("pmid", ""))
            if pmid not in flagged:
                problems.append(f"pmid {pmid!r} was not flagged by the evaluator")
            elif pmid in redo or pmid in drop:
                problems.append(f"pmid {pmid} listed twice")
            elif e.get("decision") == "redo" and (e.get("instructions") or "").strip():
                redo[pmid] = e["instructions"].strip()
            elif e.get("decision") == "drop" and (e.get("reason") or "").strip():
                drop[pmid] = e["reason"].strip()
            else:
                problems.append(f"pmid {pmid}: use decision 'redo' with instructions, or 'drop' with a reason")
        missing = [p for p in flagged if p not in redo and p not in drop]
        if missing:
            problems.append(f"no decision for: {', '.join(missing)}")
        if problems:
            return ToolReply("Round plan not accepted:\n- " + "\n- ".join(problems), True)
        state.redo, state.drop, state.accepted = redo, drop, True
        return ToolReply(f"Round plan accepted: {len(redo)} to redo, {len(drop)} dropped.")

    tool = ToolSpec(
        "submit_round_plan",
        "For each paper the evaluator flagged: redo it with instructions for the worker, or drop it with a reason.",
        {"type": "object", "properties": {"papers": {"type": "array", "items": {"type": "object", "properties": {
            "pmid": {"type": "string"},
            "decision": {"type": "string", "enum": ["redo", "drop"]},
            "instructions": {"type": "string"},
            "reason": {"type": "string"}}, "required": ["pmid", "decision"]}}},
         "required": ["papers"]},
        submit_round_plan)
    by_pmid = {str(p["pmid"]): p for p in papers}
    blocks = []
    for pmid, v in flagged.items():
        lines = [paper_block(by_pmid[pmid]), "Current records:"]
        lines += [record_line(r) for r in records_by_pmid.get(pmid, [])] or ["(none)"]
        lines += ["Evaluator issues:"] + [f"- {i}" for i in v["issues"]] + [f"Evaluator instructions: {v['instructions']}"]
        blocks.append("\n".join(lines))
    prompt = (f"Task: {task}\nThe evaluator's summary: {verdict.summary or '(none)'}\n\n"
              f"{len(flagged)} papers were flagged. Decide on each, then call submit_round_plan.\n\n" + "\n\n".join(blocks))
    call = AgentCall("orchestrator", "orchestrator: re-plan", prompts.ORCHESTRATOR, prompt, [tool],
                     settings.model, settings.max_turns, budget)
    return call, state


# --------------------------------------------------------------------------- orchestrator: synthesis

@dataclass
class SynthesisState:
    accepted: bool = False
    groups: dict[str, str] = field(default_factory=dict)   # record id -> outcome group
    summary: str = ""


def synthesis_call(task: str, papers: list[dict], records: list[dict], settings: RoleSettings,
                   budget: float | None) -> tuple[AgentCall, SynthesisState]:
    state = SynthesisState()
    ids = [r["id"] for r in records]

    async def submit_synthesis(args: dict) -> ToolReply:
        groups, problems = {}, []
        for e in _list_arg(args, "outcome_groups"):
            rid, group = str(e.get("record_id", "")), (e.get("group") or "").strip()
            if rid not in ids:
                problems.append(f"unknown record_id {rid!r}")
            elif not group or len(group) > 80:
                problems.append(f"{rid}: group must be a short label (1-80 characters)")
            else:
                groups[rid] = group
        missing = [i for i in ids if i not in groups]
        if missing:
            problems.append(f"no group for: {', '.join(missing)}")
        if not (args.get("summary") or "").strip():
            problems.append("summary is empty")
        if problems:
            return ToolReply("Synthesis not accepted:\n- " + "\n- ".join(problems), True)
        state.groups, state.summary, state.accepted = groups, args["summary"].strip(), True
        return ToolReply("Synthesis recorded.")

    tool = ToolSpec(
        "submit_synthesis",
        "Give every record a canonical outcome group and write the cross-paper summary.",
        {"type": "object", "properties": {
            "outcome_groups": {"type": "array", "items": {"type": "object", "properties": {
                "record_id": {"type": "string"}, "group": {"type": "string"}}, "required": ["record_id", "group"]}},
            "summary": {"type": "string"}},
         "required": ["outcome_groups", "summary"]},
        submit_synthesis)
    titles = {str(p["pmid"]): f"{p.get('title', '')} ({p.get('year', '')})" for p in papers}
    lines = [f"Task: {task}", "", "Final records:"]
    for r in records:
        lines.append(f"{record_line(r)} | paper: {titles.get(str(r['pmid']), r['pmid'])}")
    lines += ["", "Assign an outcome group to every record id, write the summary, then call submit_synthesis."]
    call = AgentCall("orchestrator", "orchestrator: synthesis", prompts.ORCHESTRATOR, "\n".join(lines), [tool],
                     settings.model, settings.max_turns, budget)
    return call, state
