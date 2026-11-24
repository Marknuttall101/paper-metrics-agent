"""How an agent call is run.

The pipeline describes each agent call as an ``AgentCall`` (role, prompts,
model, tools, limits) and hands it to a backend:

* ``ClaudeSDKBackend`` runs it on the Claude Agent SDK, billed to an Anthropic API key.
* ``ScriptedBackend`` (see ``scripted.py``) replays a fixed script with no model at all,
  for the offline demo and the tests.

Agents never get built-in tools (no shell, no file access, no web). The only
tools they can call are the in-process ones in ``AgentCall.tools``, which
check their input and record results in memory.
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

TOOL_SERVER = "paper_metrics"


@dataclass
class ToolReply:
    text: str
    is_error: bool = False


ToolHandler = Callable[[dict[str, Any]], Awaitable[ToolReply]]


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler


@dataclass
class AgentCall:
    role: str                     # "orchestrator", "worker" or "evaluator"
    label: str                    # e.g. "worker SAMPLE-002 round 2"
    system_prompt: str
    prompt: str
    tools: list[ToolSpec]
    model: str
    max_turns: int
    max_budget_usd: float | None = None


@dataclass
class AgentResult:
    label: str
    role: str
    model: str = ""
    cost_usd: float = 0.0
    turns: int = 0
    duration_ms: int = 0
    tool_calls: list[str] = field(default_factory=list)
    final_text: str = ""
    error: str | None = None
    api_key_source: str | None = None
    stop_subtype: str | None = None   # the SDK's result subtype, e.g. "success" or "error_max_turns"
    cost_known: bool = True           # False when the call ended before reporting its cost


class Backend(Protocol):
    name: str

    async def run(self, call: AgentCall) -> AgentResult: ...


def mcp_tool_name(tool_name: str) -> str:
    """The name Claude sees for one of our in-process tools."""
    return f"mcp__{TOOL_SERVER}__{tool_name}"


class AuthError(RuntimeError):
    """No usable credentials for the Claude Agent SDK."""


def check_api_key(allow_other_auth: bool = False) -> None:
    """Refuse to start without ANTHROPIC_API_KEY, so runs are never silently billed elsewhere.

    ``allow_other_auth`` is for people who route Claude through Amazon Bedrock,
    Google Vertex AI or another provider that Claude Code supports.
    """
    if os.environ.get("ANTHROPIC_API_KEY") or allow_other_auth:
        return
    raise AuthError(
        "ANTHROPIC_API_KEY is not set. Create a key at console.anthropic.com, then:\n"
        "  macOS/Linux:  export ANTHROPIC_API_KEY=sk-ant-...\n"
        "  Windows:      $env:ANTHROPIC_API_KEY = \"sk-ant-...\"   (PowerShell)\n"
        "Or run the offline demo, which needs no key:  paper-metrics demo"
    )


class ClaudeSDKBackend:
    """Runs agent calls with the Claude Agent SDK (``pip install claude-agent-sdk``)."""

    name = "claude-agent-sdk"

    def __init__(self, cwd: str | None = None, stderr: Callable[[str], None] | None = None):
        # Agents have no file tools, but the CLI still needs a working directory. By default each
        # call gets a fresh empty temporary one (deleted afterwards), so nothing in your project
        # - a CLAUDE.md, settings - can reach a run.
        self._cwd = cwd
        self._stderr = stderr

    def build_options(self, call: AgentCall, cwd: str | None = None):
        from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, tool

        sdk_tools = []
        for spec in call.tools:
            sdk_tools.append(tool(spec.name, spec.description, spec.input_schema)(_wrap(spec.handler)))
        server = create_sdk_mcp_server(TOOL_SERVER, tools=sdk_tools)
        return ClaudeAgentOptions(
            system_prompt=call.system_prompt,
            tools=[],                                   # no built-in tools at all
            allowed_tools=[mcp_tool_name(t.name) for t in call.tools],
            mcp_servers={TOOL_SERVER: server},
            strict_mcp_config=True,                     # ignore any MCP servers in your own config
            setting_sources=[],                         # ignore user/project settings and CLAUDE.md
            model=call.model,
            max_turns=call.max_turns,
            max_budget_usd=call.max_budget_usd,
            cwd=cwd or self._cwd,
            stderr=self._stderr,
        )

    async def run(self, call: AgentCall) -> AgentResult:
        from claude_agent_sdk import AssistantMessage, ClaudeSDKError, ResultMessage, SystemMessage, ToolUseBlock, query

        result = AgentResult(label=call.label, role=call.role, model=call.model, cost_known=False)
        start = time.monotonic()
        tmp = None if self._cwd else tempfile.TemporaryDirectory(prefix="paper-metrics-", ignore_cleanup_errors=True)
        try:
            options = self.build_options(call, cwd=tmp.name if tmp else None)
            async for msg in query(prompt=call.prompt, options=options):
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    data = msg.data or {}
                    result.api_key_source = data.get("apiKeySource")
                    result.model = data.get("model") or result.model
                elif isinstance(msg, AssistantMessage):
                    if msg.error and not result.error:
                        result.error = f"API error: {msg.error}"
                    for block in msg.content:
                        if isinstance(block, ToolUseBlock):
                            result.tool_calls.append(block.name.removeprefix(f"mcp__{TOOL_SERVER}__"))
                elif isinstance(msg, ResultMessage):
                    result.stop_subtype = msg.subtype
                    result.cost_known = msg.total_cost_usd is not None
                    result.cost_usd = msg.total_cost_usd or 0.0
                    result.turns = msg.num_turns
                    result.final_text = msg.result or ""
                    if msg.is_error:
                        detail = "; ".join(msg.errors or []) or msg.subtype
                        result.error = result.error or f"agent stopped: {detail}"
        except ClaudeSDKError as e:
            result.error = result.error or f"{type(e).__name__}: {e}"
        except Exception as e:  # keep one failed agent from taking down the whole run
            result.error = result.error or f"{type(e).__name__}: {e}"
        finally:
            if tmp:
                tmp.cleanup()
        result.duration_ms = int((time.monotonic() - start) * 1000)
        return result


def _wrap(handler: ToolHandler):
    async def sdk_handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            reply = await handler(args)
        except Exception as e:  # report bad input back to the agent instead of crashing
            reply = ToolReply(f"Tool error: {type(e).__name__}: {e}", is_error=True)
        out: dict[str, Any] = {"content": [{"type": "text", "text": reply.text}]}
        if reply.is_error:
            out["is_error"] = True
        return out
    return sdk_handler
