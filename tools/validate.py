"""Shim so Claude Code mode works without installing the package: python tools/validate.py ...

The code lives in src/paper_metrics/validate.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from paper_metrics.validate import main  # noqa: E402

sys.exit(main())
