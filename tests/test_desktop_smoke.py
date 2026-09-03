import os
from pathlib import Path
import subprocess
import sys
import tempfile


def test_desktop_and_history_with_mac_branch():
    project = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="shengnian-desktop-") as directory:
        env = dict(os.environ, VOICE_JOURNAL_DATA_ROOT=directory, PYTHONIOENCODING="utf-8")
        result = subprocess.run(
            [sys.executable, str(project / "tests/desktop_smoke_runner.py")],
            env=env, capture_output=True, text=True, encoding="utf-8", timeout=45,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert '"status": "passed"' in result.stdout
