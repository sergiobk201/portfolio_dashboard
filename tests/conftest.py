"""Test bootstrap: make pipelines/ importable (the jobs run as scripts from that folder)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipelines"))
