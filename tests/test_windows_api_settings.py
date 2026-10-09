import json
import os
from unittest import mock

from api_settings import save_key
from platform_support import load_local_api_keys


def test_windows_saved_api_survives_restart_and_keeps_environment_precedence(tmp_path):
    path = tmp_path / "runtime/api-keys.json"
    save_key(path, "DEEPSEEK_API_KEY", "synthetic-test-key")
    save_key(path, "SNAPANY_API_KEY", "synthetic-optional-key")
    with mock.patch.dict(os.environ, {}, clear=True):
        load_local_api_keys(tmp_path)
        assert os.environ["DEEPSEEK_API_KEY"] == "synthetic-test-key"
        assert os.environ["SNAPANY_API_KEY"] == "synthetic-optional-key"
    with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "environment-key"}, clear=True):
        load_local_api_keys(tmp_path)
        assert os.environ["DEEPSEEK_API_KEY"] == "environment-key"
    assert json.loads(path.read_text(encoding="utf-8"))["SNAPANY_API_KEY"] == "synthetic-optional-key"
