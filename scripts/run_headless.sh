#!/usr/bin/env bash
# Run the agent without opening an interactive session.
# Uses whatever account Claude Code is signed in with (your Claude Pro/Max plan after /login).
# Permissions come from .claude/settings.json: the tools above and edits inside output/ only.
# Usage: scripts/run_headless.sh "Smith JA" 8 "heart failure"
set -euo pipefail
cd "$(dirname "$0")/.."
AUTHOR="${1:?Give an author name, e.g. \"Smith JA\"}"
MAX="${2:-10}"
TOPIC="${3:-}"
claude -p "Follow the instructions in .claude/commands/compare-metrics.md with these arguments: \"$AUTHOR\" $MAX $TOPIC"
echo
echo "Report: output/comparison.md"
