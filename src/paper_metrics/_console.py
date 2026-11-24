"""Console set-up shared by the command-line entry points."""

from __future__ import annotations

import sys


def utf8_output() -> None:
    """Print UTF-8 even when stdout/stderr are redirected on Windows (cp1252), instead of crashing on 'β' or '·'."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
