from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows batch launcher")
def test_start_corebot_batch_check_mode():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["cmd", "/d", "/c", "start_corebot.bat", "--check"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "CHECK OK" in result.stdout

@pytest.mark.skipif(os.name != "nt", reason="Windows batch launcher")
def test_start_corebot_batch_help_lists_modes_without_command_errors():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["cmd", "/d", "/c", "start_corebot.bat", "--help"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "--check" in result.stdout
    assert "--setup" in result.stdout
    assert "not recognized" not in combined
