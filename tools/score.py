"""Shim so Claude Code mode works without installing the package: python tools/score.py ...

The code lives in src/paper_metrics/score.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from paper_metrics.score import main  # noqa: E402

sys.exit(main())
