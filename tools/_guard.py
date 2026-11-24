"""Keep Claude Code mode's file writes inside output/.

.claude/settings.json lets the agent run tools/pubmed.py and tools/compare.py without asking.
Without this check, an instruction hidden in an abstract could get the agent to pass
``--out CLAUDE.md`` (or any other path) and overwrite files outside output/.
"""
import sys
from pathlib import Path

OUTPUT_DIR = (Path(__file__).resolve().parents[1] / "output").resolve()


def _out_values(argv: list[str]) -> list[str]:
    """Every value given to --out, including argparse abbreviations such as --ou or --o=..."""
    values = []
    for i, tok in enumerate(argv):
        name, eq, value = tok.partition("=")
        if len(name) >= 3 and "--out".startswith(name):
            if eq:
                values.append(value)
            elif i + 1 < len(argv):
                values.append(argv[i + 1])
    return values


def confine_out(argv: list[str]) -> None:
    """Exit with an error if any --out path is outside the project's output/ folder."""
    for value in _out_values(argv):
        target = Path(value).resolve()
        if target != OUTPUT_DIR and OUTPUT_DIR not in target.parents:
            print(f"Refusing to write {value!r}: in Claude Code mode, --out must be inside output/.",
                  file=sys.stderr)
            sys.exit(2)
