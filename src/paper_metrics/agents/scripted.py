"""A scripted stand-in for the model, for the offline demo and the tests.

No AI is called. Each role follows a fixed script on the sample data, but it
goes through exactly the same tools, checks and loop as a real run, including
one deliberate mistake so you can watch the team catch and fix it:

  plan:    the orchestrator excludes two decoys - an astrophysics paper by a different
           "Novak L" and an editorial with no results.
  round 1: the SAMPLE-002 worker first drafts a wrong odds ratio (0.84 instead of 0.81);
           check_records rejects it and the worker corrects it, but it then leaves out
           the AUC the abstract also reports.
           The evaluator compares each paper's records with the abstract (here: with
           the answer key) and flags SAMPLE-002. The orchestrator re-plans.
  round 2: only SAMPLE-002 is redone, with both estimates. The evaluator approves.
"""

from __future__ import annotations

import json
import re
from collections import Counter

from .backend import AgentCall, AgentResult, ToolSpec

_PMID = re.compile(r'<paper pmid="([^"]+)">')
_RECORD = re.compile(r"\[([^\]]+#\d+)\] (\w+) .*?\| outcome: (.*?) \| comparison:")
_SUBMITTED = re.compile(r"Submitted records \((\d+)\)")

EXCLUSIONS = {
    "SAMPLE-006": "Astrophysics paper by a different L Novak (no shared co-authors or topic).",
    "SAMPLE-007": "Editorial with no quantitative results.",
}

OUTCOME_GROUPS = {
    "heart failure hospitalisation": "Heart failure hospitalisation",
    "all-cause mortality": "All-cause mortality",
    "cardiovascular death": "Cardiovascular death",
    "30-day readmission (adjusted)": "Readmission",
    "readmission risk model (validation set)": "Readmission risk model",
    "change in 6-minute walk distance": "Exercise capacity",
    "nt-probnp >= 900 pg/ml vs expert panel": "NT-proBNP threshold accuracy",
}


class ScriptedBackend:
    name = "scripted (no AI)"

    def __init__(self, gold: list[dict]):
        self.gold = gold
        self.gold_counts = Counter(str(r["pmid"]) for r in gold)

    async def run(self, call: AgentCall) -> AgentResult:
        result = AgentResult(label=call.label, role=call.role, model="scripted")
        tools = {t.name: t for t in call.tools}
        try:
            if "submit_plan" in tools:
                await self._plan(call, tools, result)
            elif "submit_records" in tools:
                await self._worker(call, tools, result)
            elif "submit_verdict" in tools:
                await self._evaluate(call, tools, result)
            elif "submit_round_plan" in tools:
                await self._replan(call, tools, result)
            elif "submit_synthesis" in tools:
                await self._synthesise(call, tools, result)
        except Exception as e:  # a script bug should show up, not hang the run
            result.error = f"{type(e).__name__}: {e}"
        result.turns = len(result.tool_calls)
        return result

    async def _use(self, tool: ToolSpec, args: dict, result: AgentResult, expect_error: bool = False) -> str:
        reply = await tool.handler(args)
        result.tool_calls.append(tool.name)
        if reply.is_error and not expect_error:
            raise RuntimeError(f"{tool.name} rejected the scripted input: {reply.text}")
        result.final_text = reply.text
        return reply.text

    async def _plan(self, call, tools, result):
        entries = []
        for pmid in _PMID.findall(call.prompt):
            n = self.gold_counts.get(pmid, 0)
            if n:
                entries.append({"pmid": pmid, "decision": "extract",
                                "instructions": f"Expect about {n} estimate(s); watch for numbers that are not results."})
            else:
                entries.append({"pmid": pmid, "decision": "exclude", "reason": EXCLUSIONS.get(pmid, "out of scope")})
        await self._use(tools["submit_plan"], {"papers": entries, "notes": "Five papers on examplozumab are in scope."}, result)

    async def _worker(self, call, tools, result):
        pmid, round_no = re.match(r"worker (\S+) round (\d+)", call.label).groups()
        records = [{k: v for k, v in r.items() if k not in ("pmid", "note")} for r in self.gold if str(r["pmid"]) == pmid]
        if pmid == "SAMPLE-002" and round_no == "1":
            wrong = json.loads(json.dumps(records[:1]))
            wrong[0]["value"] = 0.84                       # the deliberate slip
            await self._use(tools["check_records"], {"records": wrong}, result)
            records = records[:1]                          # ...and the missed AUC
        await self._use(tools["check_records"], {"records": records}, result)
        await self._use(tools["submit_records"], {"records": records, "notes": ""}, result)

    async def _evaluate(self, call, tools, result):
        papers = []
        blocks = call.prompt.split('<paper pmid="')[1:]
        for block in blocks:
            pmid = block.split('"', 1)[0]
            found = int(_SUBMITTED.search(block).group(1))
            expected = self.gold_counts.get(pmid, 0)
            if found < expected:
                papers.append({"pmid": pmid, "status": "redo",
                               "issues": [f"{expected - found} reported estimate(s) are missing"],
                               "instructions": "Record every estimate in the abstract, including the risk model's AUC."})
            else:
                papers.append({"pmid": pmid, "status": "ok", "issues": [], "instructions": ""})
        done = all(p["status"] == "ok" for p in papers)
        await self._use(tools["submit_verdict"], {
            "done": done, "quality_score": 95 if done else 70, "papers": papers,
            "summary": "All estimates recorded and correctly labelled." if done else "One paper is missing an estimate."},
            result)

    async def _replan(self, call, tools, result):
        entries = [{"pmid": pmid, "decision": "redo",
                    "instructions": "The abstract reports two estimates: the adjusted odds ratio and the AUC of the "
                                    "readmission risk model (0.77, 95% CI 0.74-0.80). Record both."}
                   for pmid in _PMID.findall(call.prompt)]
        await self._use(tools["submit_round_plan"], {"papers": entries}, result)

    async def _synthesise(self, call, tools, result):
        groups = [{"record_id": rid, "group": OUTCOME_GROUPS.get(outcome.strip().lower(), outcome.strip())}
                  for rid, _metric, outcome in _RECORD.findall(call.prompt)]
        summary = ("Across the five sample papers, examplozumab was linked to fewer heart failure hospitalisations "
                   "(trial HR 0.74) and lower odds of readmission (OR 0.81), but not to lower mortality: all-cause "
                   "mortality (HR 0.93) and cardiovascular death (HR 1.02) intervals include 1. The walk-distance and "
                   "diagnostic-accuracy results measure different things and are not directly comparable. "
                   "(Scripted demo - fictional data.)")
        await self._use(tools["submit_synthesis"], {"outcome_groups": groups, "summary": summary}, result)
