"""SDK wiring (without calling the API) and the command line."""

import asyncio
import json

import pytest

from paper_metrics.agents.backend import (AgentCall, AuthError, ClaudeSDKBackend, ToolReply, ToolSpec,
                                          _wrap, check_api_key, mcp_tool_name)
from paper_metrics.cli import main


async def _ok(args):
    return ToolReply("fine")


def _call():
    spec = ToolSpec("submit_x", "Submit.", {"type": "object", "properties": {}}, _ok)
    return AgentCall("worker", "worker T round 1", "system", "prompt", [spec], "haiku", 7, 0.25)


def test_sdk_options_give_agents_only_our_tools_and_no_local_settings(tmp_path):
    opts = ClaudeSDKBackend(cwd=str(tmp_path)).build_options(_call())
    assert opts.tools == []                                   # no Bash, Read, Write, WebFetch...
    assert opts.allowed_tools == [mcp_tool_name("submit_x")]
    assert opts.strict_mcp_config is True
    assert opts.setting_sources == []                         # no CLAUDE.md or user settings
    assert (opts.model, opts.max_turns, opts.max_budget_usd) == ("haiku", 7, 0.25)
    assert opts.system_prompt == "system"


def test_tool_errors_are_returned_to_the_agent_not_raised():
    async def boom(args):
        raise ValueError("'records' must be a list")
    out = asyncio.run(_wrap(boom)({}))
    assert out["is_error"] is True and "'records' must be a list" in out["content"][0]["text"]
    ok = asyncio.run(_wrap(_ok)({}))
    assert "is_error" not in ok and ok["content"][0]["text"] == "fine"


def test_api_key_is_required(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(AuthError):
        check_api_key()
    check_api_key(allow_other_auth=True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    check_api_key()


def test_cli_offline_demo(tmp_path, capsys):
    assert main(["demo", "--out", str(tmp_path / "d"), "--quiet"]) == 0
    assert "Approved by the evaluator" in capsys.readouterr().out
    assert json.loads((tmp_path / "d" / "run_report.json").read_text(encoding="utf-8"))["approved"] is True


def test_cli_run_without_a_key_fails_cleanly(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["run", "--author", "Smith JA"]) == 1
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err


def test_cli_tool_subcommands(tmp_path, capsys):
    from paper_metrics.cli import sample_file
    gold, papers = str(sample_file("gold.json")), str(sample_file("papers.json"))
    assert main(["validate", gold, "--papers", papers]) == 0
    assert main(["score", gold, "--gold", gold]) == 0
    assert main(["compare", gold, "--papers", papers, "--out", str(tmp_path / "c.md")]) == 0
    assert (tmp_path / "c.md").exists()


def test_claude_code_mode_shims_run(tmp_path):
    """tools/*.py are what Claude Code mode calls (they put src/ on the path, so no install is needed)."""
    import subprocess
    import sys
    from pathlib import Path
    from paper_metrics.cli import sample_file
    root = Path(__file__).resolve().parents[1]
    gold, papers = str(sample_file("gold.json")), str(sample_file("papers.json"))
    for args in (["tools/validate.py", gold, "--papers", papers],
                 ["tools/score.py", gold, "--gold", gold],
                 ["tools/compare.py", gold, "--papers", papers],
                 ["tools/pubmed.py", "--help"]):
        done = subprocess.run([sys.executable, *args], cwd=root, capture_output=True, text=True)
        assert done.returncode == 0, (args, done.stderr)


@pytest.mark.parametrize("out_args", [["--out", "CLAUDE.md"], ["--out=../escape.md"], ["--ou", "x.md"],
                                      ["--out", "output/../README.md"]])
def test_claude_code_mode_shims_only_write_inside_output(tmp_path, out_args):
    """An injected abstract must not be able to make the auto-allowed tools write outside output/."""
    import subprocess
    import sys
    from pathlib import Path
    from paper_metrics.cli import sample_file
    root = Path(__file__).resolve().parents[1]
    gold, papers = str(sample_file("gold.json")), str(sample_file("papers.json"))
    before = (root / "README.md").read_bytes()
    for script in (["tools/compare.py", gold, "--papers", papers], ["tools/pubmed.py", "fetch", "1"]):
        done = subprocess.run([sys.executable, *script, *out_args], cwd=root, capture_output=True, text=True)
        assert done.returncode == 2 and "must be inside output/" in done.stderr, (script, done.stderr)
    assert (root / "README.md").read_bytes() == before
    assert not (root / "x.md").exists() and not (root.parent / "escape.md").exists()


def test_claude_code_mode_shims_write_inside_output():
    import subprocess
    import sys
    from pathlib import Path
    from paper_metrics.cli import sample_file
    root = Path(__file__).resolve().parents[1]
    gold, papers = str(sample_file("gold.json")), str(sample_file("papers.json"))
    target = root / "output" / "_test_guard_comparison.md"
    try:
        done = subprocess.run([sys.executable, "tools/compare.py", gold, "--papers", papers, "--out", str(target)],
                              cwd=root, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        assert target.exists()
    finally:
        target.unlink(missing_ok=True)
