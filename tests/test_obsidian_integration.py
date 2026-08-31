from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from obsidian_integration import (  # noqa: E402
    WELCOME_NOTE_NAME,
    build_open_uri,
    ensure_welcome_note,
    registered_vault_paths,
    vault_is_registered,
)


class ObsidianIntegrationTests(unittest.TestCase):
    def test_open_uri_percent_encodes_windows_path(self):
        path = Path(r"C:\Users\测试 用户\笔记#1.md")
        uri = build_open_uri(path)
        self.assertTrue(uri.startswith("obsidian://open?path="))
        self.assertIn("%23", uri)
        self.assertIn("%20", uri)
        self.assertNotIn("测试 用户", uri)

    def test_welcome_note_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "notes"
            note = ensure_welcome_note(root)
            self.assertEqual(note.name, WELCOME_NOTE_NAME)
            note.write_text("我的内容", encoding="utf-8")
            ensure_welcome_note(root)
            self.assertEqual(note.read_text(encoding="utf-8"), "我的内容")

    def test_reads_registered_vault_without_writing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            notes = base / "我的笔记"
            notes.mkdir()
            config = base / "obsidian.json"
            config.write_text(json.dumps({"vaults": {"abc": {"path": str(notes)}}}), encoding="utf-8")
            before = config.read_bytes()

            self.assertEqual(registered_vault_paths(config), [notes.resolve()])
            self.assertTrue(vault_is_registered(notes, config))
            self.assertEqual(config.read_bytes(), before)

    def test_invalid_config_is_treated_as_not_registered(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "obsidian.json"
            config.write_text("not-json", encoding="utf-8")
            self.assertEqual(registered_vault_paths(config), [])
            self.assertFalse(vault_is_registered(Path(tmp) / "notes", config))


if __name__ == "__main__":
    unittest.main()
