"""Multi-agent pipeline: an orchestrator plans, workers extract, an evaluator decides when the job is done."""

from .backend import AgentCall, AgentResult, AuthError, ClaudeSDKBackend, ToolReply, ToolSpec, check_api_key
from .pipeline import Pipeline, RunConfig, RunOutcome
from .roles import RoleSettings
from .scripted import ScriptedBackend

__all__ = [
    "AgentCall", "AgentResult", "AuthError", "ClaudeSDKBackend", "Pipeline", "RoleSettings",
    "RunConfig", "RunOutcome", "ScriptedBackend", "ToolReply", "ToolSpec", "check_api_key",
]
