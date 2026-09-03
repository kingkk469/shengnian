from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import platform_support as ps
TEST_HOME = str(Path.home())


class PlatformSupportTests(TestCase):
    def platform(self, name):
        return mock.patch.object(ps, "sys", SimpleNamespace(platform=name, executable=sys.executable))

    def test_mac_data_path_ignores_windows_environment(self):
        with self.platform("darwin"), mock.patch.dict(os.environ, {"LOCALAPPDATA": "wrong", "USERPROFILE": TEST_HOME, "HOME": TEST_HOME}, clear=True):
            self.assertEqual(ps.default_data_root(), Path.home() / "Library/Application Support/VoiceJournal/Data")

    def test_windows_default_data_path_is_preserved(self):
        with self.platform("win32"), mock.patch.dict(os.environ, {"LOCALAPPDATA": "local-data"}, clear=True):
            self.assertEqual(ps.default_data_root(), Path("local-data/VoiceJournal/Data"))

    def test_explicit_data_root_wins_on_both_systems(self):
        for platform_name in ("darwin", "win32"):
            with self.platform(platform_name), mock.patch.dict(os.environ, {"VOICE_JOURNAL_DATA_ROOT": "~/custom-data", "USERPROFILE": TEST_HOME, "HOME": TEST_HOME}, clear=True):
                self.assertEqual(ps.default_data_root(), Path.home() / "custom-data")

    def test_mac_python_uses_venv_with_spaces(self):
        with tempfile.TemporaryDirectory() as directory, self.platform("darwin"):
            root = Path(directory) / "声年 Mac"
            binary = root / ".venv/bin/python"
            binary.parent.mkdir(parents=True)
            binary.touch()
            self.assertEqual(ps.source_python(root), binary)

    def test_python_falls_back_to_running_interpreter(self):
        with tempfile.TemporaryDirectory() as directory, self.platform("darwin"):
            self.assertEqual(ps.source_python(Path(directory)), Path(sys.executable))

    def test_mac_open_keeps_shell_characters_literal(self):
        path = Path("声年 user's notes $(touch nope).md").resolve()
        with self.platform("darwin"), mock.patch.object(ps.subprocess, "run") as run:
            ps.open_path(path)
            run.assert_called_once_with(["/usr/bin/open", str(path)], check=True, timeout=10)

    def test_qt_platform_is_only_forced_on_windows(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.platform("darwin"):
                ps.configure_qt_environment()
                self.assertNotIn("QT_QPA_PLATFORM", os.environ)
            with self.platform("win32"):
                ps.configure_qt_environment()
                self.assertEqual(os.environ["QT_QPA_PLATFORM"], "windows:fontengine=freetype")

    def test_qt_explicit_offscreen_is_preserved(self):
        with self.platform("win32"), mock.patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}):
            ps.configure_qt_environment()
            self.assertEqual(os.environ["QT_QPA_PLATFORM"], "offscreen")

    def test_model_cpu_default_is_mac_only(self):
        with self.platform("darwin"):
            self.assertEqual(ps.model_device_kwargs({}), {"device": "cpu"})
            self.assertEqual(ps.model_device_kwargs({"device": "mps"}), {"device": "mps"})
        with self.platform("win32"):
            self.assertEqual(ps.model_device_kwargs({}), {})

    def test_api_file_cannot_override_env_or_inject_other_settings(self):
        with tempfile.TemporaryDirectory() as directory, self.platform("darwin"), mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "environment-test"}, clear=True):
            root = Path(directory)
            (root / "runtime").mkdir()
            (root / "runtime/api-keys.json").write_text(json.dumps({"DEEPSEEK_API_KEY": "file-test", "SNAPANY_API_KEY": "optional-test", "PATH": "bad"}), encoding="utf-8")
            ps.load_local_api_keys(root)
            self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "environment-test")
            self.assertEqual(os.environ["SNAPANY_API_KEY"], "optional-test")
            self.assertNotIn("PATH", os.environ)

    def test_invalid_api_file_is_tolerated(self):
        with tempfile.TemporaryDirectory() as directory, self.platform("darwin"), mock.patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            (root / "runtime").mkdir()
            for content in ("not json", "[]", "null"):
                (root / "runtime/api-keys.json").write_text(content)
                ps.load_local_api_keys(root)
                self.assertNotIn("DEEPSEEK_API_KEY", os.environ)

    def test_caffeinate_is_idempotent_and_released(self):
        child = mock.Mock()
        child.poll.return_value = None
        with self.platform("darwin"), mock.patch.object(ps, "_sleep_process", None), mock.patch.object(ps.subprocess, "Popen", return_value=child) as popen:
            ps.prevent_macos_sleep(True)
            ps.prevent_macos_sleep(True)
            popen.assert_called_once()
            self.assertEqual(popen.call_args.args[0], ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])
            ps.prevent_macos_sleep(False)
            child.terminate.assert_called_once()
            child.wait.assert_called_once_with(timeout=3)

    def test_current_process_exists(self):
        self.assertTrue(ps.pid_exists(os.getpid()))
        self.assertFalse(ps.pid_exists(-1))

    def test_posix_role_lock_blocks_another_process_and_recovers(self):
        if os.name == "nt":
            self.skipTest("fcntl requires POSIX; run this test on Mac")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = ps.RoleLock(root, "recorder")
            self.assertTrue(lock.acquire())
            try:
                self.assertEqual(ps.locked_role_pid(root, "recorder"), os.getpid())
                code = "import sys; from pathlib import Path; from platform_support import RoleLock; lock=RoleLock(Path(sys.argv[1]), 'recorder'); sys.exit(0 if lock.acquire() else 3)"
                env = {**os.environ, "PYTHONPATH": str(Path(ps.__file__).parent)}
                result = subprocess.run([sys.executable, "-c", code, str(root)], env=env)
                self.assertEqual(result.returncode, 3)
            finally:
                lock.release()
            self.assertIsNone(ps.locked_role_pid(root, "recorder"))
            result = subprocess.run([sys.executable, "-c", code, str(root)], env=env)
            self.assertEqual(result.returncode, 0)

    def test_stale_unlocked_pid_file_is_not_adopted(self):
        if os.name == "nt":
            self.skipTest("fcntl requires POSIX; run this test on Mac")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "runtime/recorder.lock"
            path.parent.mkdir()
            path.write_text(json.dumps({"pid": os.getpid(), "birth": ps._process_birth(os.getpid())}))
            self.assertIsNone(ps.locked_role_pid(root, "recorder"))


def load_mac_module(name):
    path = Path(__file__).resolve().parents[1] / "macos" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"macos_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MacInstallerTests(TestCase):
    def test_api_save_preserves_unrelated_key_and_rejects_corruption(self):
        module = load_mac_module("configure")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api-keys.json"
            module.save_key(path, "SNAPANY_API_KEY", "optional-test")
            module.save_key(path, "DEEPSEEK_API_KEY", "deepseek-test")
            data = json.loads(path.read_text())
            self.assertEqual(data["SNAPANY_API_KEY"], "optional-test")
            self.assertEqual(data["DEEPSEEK_API_KEY"], "deepseek-test")
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.write_text("broken")
            with self.assertRaises(ValueError):
                module.save_key(path, "DEEPSEEK_API_KEY", "new-test")
            self.assertEqual(path.read_text(), "broken")

    def test_launch_agent_keeps_unicode_path_as_one_argument(self):
        import plistlib
        module = load_mac_module("autostart")
        project = Path("/Users/测试 用户/声年")
        data = plistlib.loads(plistlib.dumps(module.launch_agent(project)))
        self.assertEqual(data["ProgramArguments"][-1], str(project / "start-macos.command"))
        self.assertNotIn("KeepAlive", data)
        self.assertTrue(data["RunAtLoad"])
