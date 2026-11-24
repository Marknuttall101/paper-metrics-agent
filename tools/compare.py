"""Shim so Claude Code mode works without installing the package: python tools/compare.py ...

The code lives in src/paper_metrics/compare.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from paper_metrics.compare import main  # noqa: E402

from _guard import confine_out  # noqa: E402

confine_out(sys.argv[1:])
sys.exit(main())
