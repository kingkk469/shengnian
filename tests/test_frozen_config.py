import os
from pathlib import Path
import subprocess
import sys
import tempfile


def test_frozen_config_and_defaults_stay_outside_application():
    project = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        resources = base / "app-resources"
        data = base / "user-data"
        (resources / "defaults").mkdir(parents=True)
        (resources / "config.example.toml").write_bytes((project / "src/config.example.toml").read_bytes())
        (resources / "defaults/hotwords.txt").write_text("声年", encoding="utf-8")
        code = """
import sys
from pathlib import Path
sys.frozen = True
sys._MEIPASS = sys.argv[1]
import common
assert common.ROOT == Path(sys.argv[2])
assert (common.ROOT / 'config.toml').is_file()
assert (common.ROOT / 'hotwords.txt').read_text(encoding='utf-8') == '声年'
assert not (Path(sys._MEIPASS) / 'config.toml').exists()
"""
        env = dict(os.environ, VOICE_JOURNAL_DATA_ROOT=str(data), PYTHONPATH=str(project / "src"), PYTHONIOENCODING="utf-8")
        env.pop("VOICE_JOURNAL_CONFIG", None)
        command = [sys.executable, "-c", code, str(resources), str(data)]
        result = subprocess.run(command, env=env, capture_output=True, timeout=20)
        assert result.returncode == 0, result.stderr
        configured = data / "config.toml"
        original = configured.read_bytes()
        configured.write_bytes(original + b"\n# preserve user edits\n")
        result = subprocess.run(command, env=env, capture_output=True, timeout=20)
        assert result.returncode == 0, result.stderr
        assert configured.read_bytes().endswith(b"# preserve user edits\n")
