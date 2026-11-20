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
            stage.mkdir(parents=True)
            with patch.object(config, "OUTPUT_DIR", root):
                _publish([], {}, {"status": "采集完成"}, stage, lambda _: None)
            self.assertFalse(review.exists())
            self.assertEqual(list(assets.iterdir()), [])
            self.assertEqual(json.loads((root / "research.json").read_text(encoding="utf-8"))["assets"], [])

    def test_cleanup_does_not_create_review_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "delivery"
            root.mkdir()
            remove_legacy_review(root)
            self.assertFalse((root / "review").exists())

    def test_empty_run_replaces_previous_assets_and_manifests(self):
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
            self.assertTrue(assets.is_dir())
            self.assertEqual(list(assets.iterdir()), [])
            self.assertFalse(old_file.exists())
            self.assertEqual(json.loads(research.read_text(encoding="utf-8")),
                             {"schema_version": "1.0", "city": config.CITY, "request": "", "assets": []})
            self.assertEqual(json.loads((root / "fact.json").read_text(encoding="utf-8")),
                             {"schema_version": "1.0", "city": config.CITY, "facts": []})
            self.assertEqual(json.loads((root / "sources.json").read_text(encoding="utf-8")),
                             {"schema_version": "1.0", "city": config.CITY, "sources": []})
            self.assertTrue((root / "collection_report.json").is_file())
            self.assertIn("仅保留通过审核的素材", (root / "README.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
