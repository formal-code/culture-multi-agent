import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from culture.collector import _publish, remove_legacy_review
from culture.types import Article, Media


class DeliveryTests(unittest.TestCase):
    def test_retired_review_directory_removed_without_touching_shared_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "delivery"
            assets = root / "assets"
            assets.mkdir(parents=True)
            (assets / "old.jpg").write_bytes(b"old approved")
            (root / "research.json").write_text("previous package", encoding="utf-8")
            review = root / "review"
            review.mkdir()
            (review / "old.jpg").write_bytes(b"pending")
            (review / "old.mp4").write_bytes(b"pending video")
            stage = Path(temp) / "stage"
            with patch.object(config, "OUTPUT_DIR", root):
                _publish([], {}, {"status": "采集完成"}, stage, lambda _: None)
            self.assertFalse(review.exists())
            self.assertEqual((assets / "old.jpg").read_bytes(), b"old approved")
            self.assertEqual((root / "research.json").read_text(encoding="utf-8"), "previous package")

    def test_cleanup_does_not_create_review_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "delivery"
            root.mkdir()
            remove_legacy_review(root)
            self.assertFalse((root / "review").exists())

    def test_empty_run_preserves_previous_assets_and_manifests(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "data"
            assets = root / "assets"
            assets.mkdir(parents=True)
            old_file = assets / "old.jpg"
            old_file.write_bytes(b"previous shared asset")
            research = root / "research.json"
            research.write_text("previous metadata", encoding="utf-8")
            stage = Path(temp) / "stage"
            stage.mkdir()
            report = {"status": "采集完成", "selected_asset_ids": []}
            with patch.object(config, "OUTPUT_DIR", root):
                _publish([], {}, report, stage, lambda _: None)
            self.assertEqual(old_file.read_bytes(), b"previous shared asset")
            self.assertEqual(research.read_text(encoding="utf-8"), "previous metadata")
            self.assertTrue((root / "collection_report.json").is_file())


if __name__ == "__main__":
    unittest.main()
