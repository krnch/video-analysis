import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from result_consumer import BundleError, MockConsumer


class MockConsumerTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.database = self.root / "consumer.sqlite"

    def make_bundle(self, name="bundle"):
        bundle = self.root / name
        bundle.mkdir()
        clips = []
        for index, content in enumerate((b"synthetic clip one", b"synthetic clip two"), 1):
            filename = f"clip-{index:03d}.mp4"
            (bundle / filename).write_bytes(content)
            clips.append({
                "id": uuid.uuid4().hex,
                "filename": filename,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "duration_seconds": 1.0,
                "range_seconds": {"start": 0.0, "end": 1.0},
                "captions_available": False,
            })
        manifest = {
            "schema_version": 3,
            "task_id": str(uuid.uuid4()),
            "core_version": "1.0.0",
            "source": {
                "provenance": "local_file",
                "filename": "synthetic-source.mp4",
                "sha256": hashlib.sha256(b"synthetic source").hexdigest(),
            },
            "clips": clips,
        }
        self.write_manifest(bundle, manifest)
        return bundle, manifest

    @staticmethod
    def write_manifest(bundle, manifest):
        (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_out_of_order_import_is_idempotent_and_preserves_ratings(self):
        bundle, manifest = self.make_bundle()
        manifest["clips"].reverse()
        manifest["clips"][0]["model_scores"] = [{
            "model": "synthetic-ranker",
            "score": 0.82,
            "provenance": "local test model v1",
        }]
        self.write_manifest(bundle, manifest)
        consumer = MockConsumer(self.database)

        result = consumer.import_bundle(bundle)
        self.assertEqual(result["clips_imported"], 2)
        clip_id = manifest["clips"][0]["id"]
        consumer.set_rating(clip_id, {"stars": 5, "note": "keep"})

        self.assertEqual(
            consumer.import_bundle(bundle),
            {"task_id": manifest["task_id"], "clips_imported": 0},
        )
        self.assertEqual(consumer.get_rating(clip_id), {"stars": 5, "note": "keep"})

    def test_model_scores_require_provenance(self):
        bundle, manifest = self.make_bundle()
        manifest["clips"][0]["model_scores"] = [{
            "model": "synthetic-ranker",
            "score": 0.82,
        }]
        self.write_manifest(bundle, manifest)

        with self.assertRaisesRegex(BundleError, "model, score, and provenance"):
            MockConsumer(self.database).import_bundle(bundle)

    def test_rejects_duplicate_clip_ids_without_partial_import(self):
        bundle, manifest = self.make_bundle()
        manifest["clips"][1]["id"] = manifest["clips"][0]["id"]
        self.write_manifest(bundle, manifest)
        consumer = MockConsumer(self.database)

        with self.assertRaisesRegex(BundleError, "duplicate clip id"):
            consumer.import_bundle(bundle)

        manifest["clips"][1]["id"] = uuid.uuid4().hex
        self.write_manifest(bundle, manifest)
        self.assertEqual(consumer.import_bundle(bundle)["clips_imported"], 2)

    def test_rejects_corrupt_or_partial_bundle_without_import(self):
        consumer = MockConsumer(self.database)
        corrupt_bundle, corrupt_manifest = self.make_bundle("corrupt")
        (corrupt_bundle / corrupt_manifest["clips"][0]["filename"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(BundleError, "integrity"):
            consumer.import_bundle(corrupt_bundle)

        partial_bundle, partial_manifest = self.make_bundle("partial")
        (partial_bundle / partial_manifest["clips"][1]["filename"]).unlink()
        with self.assertRaisesRegex(BundleError, "missing a clip"):
            consumer.import_bundle(partial_bundle)

    def test_rejects_conflicting_reimport_of_task(self):
        bundle, manifest = self.make_bundle()
        consumer = MockConsumer(self.database)
        consumer.import_bundle(bundle)

        manifest["clips"][0]["captions_available"] = True
        self.write_manifest(bundle, manifest)
        with self.assertRaisesRegex(BundleError, "different bundle content"):
            consumer.import_bundle(bundle)
        self.assertIsNone(consumer.get_rating(manifest["clips"][0]["id"]))


if __name__ == "__main__":
    unittest.main()
