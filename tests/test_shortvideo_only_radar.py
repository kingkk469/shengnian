import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from launcher import ContentRadarWorker  # noqa: E402


class ShortVideoOnlyRadarTests(unittest.TestCase):
    def test_manual_radar_never_runs_article_aggregation(self):
        calls = {"shortvideo": 0, "article": 0}

        def extract_from_day(_day, force=False):
            calls["shortvideo"] += 1
            self.assertTrue(force)
            return {"added": 2}

        def aggregate_themes(force=False):
            calls["article"] += 1
            raise AssertionError("新版不应调用公众号聚合")

        fake_radar = SimpleNamespace(
            extract_from_day=extract_from_day,
            aggregate_themes=aggregate_themes,
        )
        results = []
        failures = []
        worker = ContentRadarWorker()
        worker.done.connect(results.append)
        worker.failed.connect(failures.append)

        with patch.dict(sys.modules, {"content_radar": fake_radar}):
            worker.run()

        self.assertEqual(calls, {"shortvideo": 1, "article": 0})
        self.assertEqual(results, [{"sv_added": 2, "sv": {"added": 2}}])
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
