"""Shared helpers for the offline tests: no network, no API keys, nothing posted or sent.

    pip install -r requirements.txt pytest
    python -m pytest            # from the repo root, ~seconds

The tools live in folders with hyphens (tools/caption-maker/...), so they are loaded by path, once per test session.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_loaded: dict[str, object] = {}


def load(relpath: str):
    """A module from a file path, e.g. load("tools/report/report.py")."""
    if relpath not in _loaded:
        path = ROOT / relpath
        sys.path.insert(0, str(path.parent))  # a tool may import a sibling file
        spec = importlib.util.spec_from_file_location(f"vg_{path.parent.name}_{path.stem}".replace("-", "_"), path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _loaded[relpath] = mod
    return _loaded[relpath]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No run-mode or --set overlay leaks into a test, and no real token is ever in reach."""
    for k in ("VG_MODE_FILE", "VG_SET_FILE", "VG_MASTER"):
        monkeypatch.delenv(k, raising=False)
    for k in list(os.environ):
        if k.endswith(("_TOKEN", "_API_KEY", "_SECRET")) or k in ("IG_USER_ID", "FB_PAGE_ID", "THREADS_USER_ID"):
            monkeypatch.delenv(k, raising=False)


@pytest.fixture
def run_py():
    return load("run.py")


@pytest.fixture
def reel_folder(tmp_path):
    """A finished reel's folder as the publishers see it (description, script, a tiny fake video)."""
    f = tmp_path / "2026-11-15"
    f.mkdir()
    (f / "reel.mp4").write_bytes(b"\x00" * 2048)
    (f / "script.json").write_text('{"title": "Interview prep | Test", "caption": "", "lines": []}', encoding="utf-8")
    (f / "description.txt").write_text(
        "Why should we hire you answer | HR interview\n"
        "Interview prep 🎯\n"
        "Body line.\n\n"
        "Job in this episode:\n"
        "- Data Analyst at Infosys (Bengaluru, posted 2026-11-14)\n"
        "  Apply: https://career.infosys.com/joblist?postingId=1\n\n"
        "N8n: https://n8n.io/workflows/1\n"
        "Fresh jobs from company career pages, every day: link in bio.\n\n"
        "#interviewtips #hrinterview #fresherjobs\n",
        encoding="utf-8")
    return f
