from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import common
import daily_summary


class ObsidianPathResolutionTests(unittest.TestCase):
    def test_commercial_onboarding_path_is_used_when_config_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "vault"
            runtime = root / "runtime"
            runtime.mkdir()
            (runtime / "local-paths.json").write_text(
                json.dumps({"obsidian_vault": str(vault)}),
                encoding="utf-8",
            )
            with (
                patch.object(common, "ROOT", root),
                patch.dict(common.CONFIG["obsidian"], {"vault": ""}, clear=True),
            ):
                self.assertEqual(common.configured_obsidian_vault(), vault.resolve())
                self.assertEqual(common.knowledge_dir(), (vault / "第二大脑").resolve())

    def test_explicit_config_keeps_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configured = root / "configured-vault"
            runtime = root / "runtime"
            runtime.mkdir()
            (runtime / "local-paths.json").write_text(
                json.dumps({"obsidian_vault": str(root / "onboarding-vault")}),
                encoding="utf-8",
            )
            with (
                patch.object(common, "ROOT", root),
                patch.dict(
                    common.CONFIG["obsidian"],
                    {"vault": str(configured)},
                    clear=True,
                ),
            ):
                self.assertEqual(
                    common.configured_obsidian_vault(), configured.resolve()
                )

    def test_daily_summary_uses_shared_vault_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            with (
                patch.object(
                    daily_summary,
                    "configured_obsidian_vault",
                    return_value=vault,
                ),
                patch.dict(
                    daily_summary.CONFIG["obsidian"],
                    {"folder": "语音日记"},
                    clear=True,
                ),
            ):
                result = daily_summary.obsidian_note_path(date(2026, 8, 15))
            self.assertEqual(result, vault / "语音日记" / "2026-08-15.md")

    def test_local_recovery_keeps_transcript_and_marks_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note = root / "notes" / "2026-08-13.md"
            note.parent.mkdir()
            segments = [
                {
                    "start": "2026-08-13T09:00:00",
                    "text": "今天继续做真实工作。",
                    "duration_sec": 12,
                }
            ]
            minis = [{"summary_text": "这是已经完成的本地分时段小结。"}]
            with (
                patch.object(daily_summary, "ROOT", root),
                patch.object(daily_summary, "note_path", return_value=note),
                patch.object(daily_summary, "load_segments", return_value=segments),
                patch.object(daily_summary, "list_mini_summaries", return_value=minis),
                patch.object(daily_summary, "write_obsidian"),
            ):
                result = daily_summary.recover_local_note(date(2026, 8, 13))

            self.assertEqual(result, note)
            content = note.read_text(encoding="utf-8")
            self.assertIn("summary_status: pending_cloud", content)
            self.assertIn("这是已经完成的本地分时段小结", content)
            self.assertIn("今天继续做真实工作", content)
            self.assertTrue((root / "notes" / "2026-08-13.pending.json").exists())


if __name__ == "__main__":
    unittest.main()
