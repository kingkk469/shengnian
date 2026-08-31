from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import ingest_url
import snapany_client


class FakeResponse:
    def __init__(self, status: int, payload=None, *, headers=None, chunks=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self._chunks = chunks or []

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def iter_content(self, chunk_size=0):
        del chunk_size
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeSession:
    def __init__(self, post_response, get_responses=None):
        self.post_response = post_response
        self.get_responses = list(get_responses or [])
        self.post_calls = []
        self.get_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.post_response

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.get_responses.pop(0)


class SnapAnyWechatChannelsTests(unittest.TestCase):
    def test_source_detection_distinguishes_channels_from_articles(self):
        self.assertEqual(
            ingest_url.detect_source("https://weixin.qq.com/sph/AbC_123"),
            "wechat_channels",
        )
        self.assertEqual(
            ingest_url.detect_source("https://mp.weixin.qq.com/s/abc"),
            "wechat",
        )

    def test_only_public_sph_share_urls_are_accepted(self):
        self.assertEqual(
            snapany_client.validate_wechat_channels_url(
                "https://weixin.qq.com/sph/AbC-123?scene=1"
            ),
            "https://weixin.qq.com/sph/AbC-123?scene=1",
        )
        with self.assertRaisesRegex(snapany_client.SnapAnyError, "只支持"):
            snapany_client.validate_wechat_channels_url("https://example.com/video")

    def test_missing_key_never_calls_api(self):
        session = FakeSession(FakeResponse(200, {"data": {}}))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(snapany_client.SnapAnyError, "SNAPANY_API_KEY"):
                snapany_client.extract_post(
                    "https://weixin.qq.com/sph/AbC123",
                    session=session,
                )
        self.assertEqual(session.post_calls, [])

    def test_extract_uses_official_endpoint_and_bearer_key(self):
        session = FakeSession(FakeResponse(200, {"data": {"title": "测试视频"}}))
        data = snapany_client.extract_post(
            "https://weixin.qq.com/sph/AbC123",
            api_key="sk_snapany_test",
            session=session,
        )
        self.assertEqual(data["title"], "测试视频")
        url, kwargs = session.post_calls[0]
        self.assertEqual(url, snapany_client.API_ENDPOINT)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk_snapany_test")
        self.assertEqual(kwargs["json"], {"url": "https://weixin.qq.com/sph/AbC123"})

    def test_download_copies_returned_headers_exactly(self):
        payload = {
            "data": {
                "title": "视频标题",
                "author": {"name": "作者"},
                "medias": [{
                    "media_type": "video",
                    "resource_url": "https://finder.video.qq.com/signed",
                    "headers": {"Referer": "", "User-Agent": "SnapAny-Test"},
                }],
            }
        }
        session = FakeSession(
            FakeResponse(200, payload),
            [FakeResponse(
                200,
                headers={"Content-Type": "video/mp4", "Content-Length": "2048"},
                chunks=[b"0" * 2048],
            )],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path, meta = snapany_client.download_wechat_channels(
                "https://weixin.qq.com/sph/AbC123",
                Path(tmp),
                api_key="sk_snapany_test",
                session=session,
            )
            self.assertTrue(Path(path).exists())
            self.assertEqual(meta["title"], "视频标题")
            self.assertEqual(meta["author"], "作者")
        _, kwargs = session.get_calls[0]
        self.assertEqual(kwargs["headers"], {"Referer": "", "User-Agent": "SnapAny-Test"})

    def test_api_business_error_is_translated(self):
        session = FakeSession(FakeResponse(
            402,
            {"code": "insufficient_credits", "message": "no credits"},
        ))
        with self.assertRaisesRegex(snapany_client.SnapAnyError, "点数不足"):
            snapany_client.extract_post(
                "https://weixin.qq.com/sph/AbC123",
                api_key="sk_snapany_test",
                session=session,
            )

    def test_ingest_transcribes_archives_and_deletes_temporary_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "temp.mp4"
            video.write_bytes(b"video")
            with (
                patch.object(
                    snapany_client,
                    "download_wechat_channels",
                    return_value=(str(video), {"title": "测试", "author": "作者"}),
                ),
                patch.object(
                    ingest_url,
                    "_transcribe_douyin_video",
                    return_value=[{"start": 0, "end": 0, "text": "本地转写正文"}],
                ),
                patch.object(ingest_url, "_scrub_recent_transcripts", return_value=0),
                patch.object(ingest_url, "_post_ingest_to_wiki", return_value="锚点") as save_wiki,
                patch.object(ingest_url, "_post_ingest_baokuan", return_value="分析.md") as save_analysis,
                patch.object(ingest_url, "day_dir", return_value=Path(tmp)),
            ):
                result = ingest_url.ingest_wechat_channels(
                    "https://weixin.qq.com/sph/AbC123"
                )
            self.assertEqual(result["source"], "wechat_channels")
            self.assertFalse(video.exists())
            save_wiki.assert_called_once()
            save_analysis.assert_called_once()


if __name__ == "__main__":
    unittest.main()
