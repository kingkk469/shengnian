from __future__ import annotations

import os
import inspect
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import ingest_url


class IngestSecurityTests(unittest.TestCase):
    def test_commercial_profile_never_reads_browser_cookie_sources(self):
        with patch.dict(os.environ, {"VOICE_JOURNAL_BUILD_PROFILE": "commercial"}):
            self.assertEqual(ingest_url._cookie_browsers(), [])
            with patch.object(ingest_url.log, "info"):
                self.assertIsNone(ingest_url._cookies_via_rookiepy(".example.com"))
            with self.assertRaisesRegex(RuntimeError, "自动读取浏览器登录态已关闭"):
                ingest_url._read_douyin_cookies()

    def test_commercial_douyin_download_does_not_read_browser_cookies(self):
        source = inspect.getsource(ingest_url._douyin_download_async)
        self.assertNotIn("_read_douyin_cookies", source)
        self.assertIn("www.iesdouyin.com/share/video", source)

    def test_parse_public_douyin_share_page(self):
        item = {
            "aweme_id": "123456789",
            "desc": "公开视频",
            "video": {
                "duration": 3200,
                "play_addr": {"url_list": ["https://example.test/playwm/"]},
            },
        }
        router_data = {"loaderData": {"videoInfoRes": {"item_list": [item]}}}
        html = (
            "<html><script>window._ROUTER_DATA = "
            + json.dumps(router_data, ensure_ascii=False)
            + "</script></html>"
        )
        self.assertEqual(
            ingest_url._parse_douyin_share_page(html, "123456789"),
            item,
        )
        self.assertIsNone(
            ingest_url._parse_douyin_share_page(html, "987654321")
        )

    def test_frozen_douyin_transcription_uses_bundled_transcriber(self):
        bundled = types.ModuleType("transcriber")
        bundled.transcribe_wav = lambda _path: "包内模型转写成功"

        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "sample.mp4"
            video.write_bytes(b"video")

            def fake_ffmpeg(args, **_kwargs):
                Path(args[-1]).write_bytes(b"wav")
                return None

            with (
                patch.object(sys, "frozen", True, create=True),
                patch.dict(sys.modules, {"transcriber": bundled}),
                patch("audio_import._ffmpeg_executable", return_value="bundled-ffmpeg"),
                patch.object(ingest_url.subprocess, "run", side_effect=fake_ffmpeg),
            ):
                self.assertEqual(
                    ingest_url._transcribe_douyin_video(str(video)),
                    [{"start": 0, "end": 0, "text": "包内模型转写成功"}],
                )

    def test_empty_douyin_transcript_is_reported_as_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "empty.mp4"
            video.write_bytes(b"video")
            meta = {"title": "测试", "author": "作者"}
            with (
                patch.object(
                    ingest_url,
                    "_download_douyin_via_api",
                    return_value=(str(video), meta),
                ),
                patch.object(ingest_url, "_transcribe_douyin_video", return_value=[]),
                patch.object(ingest_url, "_post_ingest_to_wiki") as save_wiki,
                patch.object(ingest_url, "_post_ingest_baokuan") as save_baokuan,
            ):
                with self.assertRaisesRegex(RuntimeError, "没有生成文字"):
                    ingest_url.ingest_douyin("https://www.douyin.com/video/123")
                save_wiki.assert_not_called()
                save_baokuan.assert_not_called()

    def test_local_obsidian_path_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            runtime.mkdir()
            vault = root / "vault"
            (runtime / "local-paths.json").write_text(
                json.dumps({"obsidian_vault": str(vault)}),
                encoding="utf-8",
            )
            with patch.object(ingest_url, "ROOT", root):
                self.assertEqual(ingest_url._configured_obsidian_vault(), vault)
                self.assertEqual(
                    ingest_url._baokuan_folder(),
                    vault / "爆款分析",
                )


if __name__ == "__main__":
    unittest.main()
