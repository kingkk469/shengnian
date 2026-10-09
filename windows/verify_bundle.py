"""Run frozen tests with fresh user data and empty model caches."""
from pathlib import Path
import argparse
import json
import os
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument("--root", type=Path, required=True)
args = parser.parse_args()
root = args.root.resolve()
app = root / "dist/声年/声年.exe"
data = root / "verification/user-data"
assert app.is_file()
assert not data.exists(), "Use a fresh verification directory"
env = dict(os.environ)
for name in list(env):
    if name.endswith("_API_KEY") or name in {"VOICE_JOURNAL_CONFIG", "VOICE_JOURNAL_LOCAL_APPDATA"}:
        env.pop(name)
env.update(
    VOICE_JOURNAL_DATA_ROOT=str(data),
    HF_HOME=str(root / "verification/empty-hf-cache"),
    MODELSCOPE_CACHE=str(root / "verification/empty-modelscope-cache"),
    HF_HUB_OFFLINE="1", HF_DATASETS_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
    HTTP_PROXY="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9",
)
results = []
for mode in ("basic", "ui", "api-reload", "models"):
    process = subprocess.run(
        [str(app), "--role", "windows-self-test", "--mode", mode],
        env=env, timeout=240, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    report = data / "runtime" / f"windows-self-test-{mode}.json"
    if process.returncode != 0 or not report.is_file():
        error = data / "logs/windows-startup-error.log"
        raise RuntimeError(f"{mode} failed ({process.returncode}): " + (error.read_text(encoding="utf-8") if error.exists() else "no report"))
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "passed"
    results.append(payload)
    print(json.dumps(payload, ensure_ascii=False), flush=True)
(root / "verification/RESULTS.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
