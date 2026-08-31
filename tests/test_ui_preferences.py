import json
import sys
import tempfile
import unittest
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from ui_preferences import (  # noqa: E402
    load_font_scale,
    save_font_scale,
    scale_stylesheet_font_sizes,
)


class UiPreferenceTests(unittest.TestCase):
    def test_font_scale_round_trip_is_local_and_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(load_font_scale(root), 1.0)
            self.assertEqual(save_font_scale(root, 1.14), 1.15)
            self.assertEqual(load_font_scale(root), 1.15)
            payload = json.loads(
                (root / "runtime" / "ui-preferences.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                set(payload),
                {"schema_version", "font_scale"},
            )

    def test_stylesheet_scaling_changes_only_font_sizes(self):
        source = (
            "QLabel { font-size: 10px; padding: 8px; } "
            "QPushButton { FONT-SIZE: 12.5px; min-width: 100px; }"
        )
        scaled = scale_stylesheet_font_sizes(source, 1.15)
        self.assertIn("font-size: 11.5px", scaled)
        self.assertIn("FONT-SIZE: 14.38px", scaled)
        self.assertIn("padding: 8px", scaled)
        self.assertIn("min-width: 100px", scaled)


if __name__ == "__main__":
    unittest.main()
