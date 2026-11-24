# Run the agent without opening an interactive session (Windows PowerShell).
# Uses whatever account Claude Code is signed in with (your Claude Pro/Max plan after /login).
# Permissions come from .claude/settings.json: the tools above and edits inside output/ only.
# Usage: .\scripts\run_headless.ps1 -Author "Smith JA" -Max 8 -Topic "heart failure"
param(
  [Parameter(Mandatory = $true)][string]$Author,
  [int]$Max = 10,
  [string]$Topic = ""
)
Set-Location (Join-Path $PSScriptRoot "..")
claude -p "Follow the instructions in .claude/commands/compare-metrics.md with these arguments: `"$Author`" $Max $Topic"
Write-Output ""
Write-Output "Report: output/comparison.md"
