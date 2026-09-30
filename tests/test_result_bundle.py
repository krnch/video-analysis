import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from mock_consumer import ConsumerError, MockConsumer
from result_bundle import (
    BundleError,
    CORE_VERSION,
    build_bundle,
    validate_bundle,
)
from video_clipper import clip


class ResultBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.source = Path(cls.fixture.name) / "source.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
             "testsrc2=size=160x120:rate=25", "-t", "3",
             "-c:v", "mpeg4", "-y", str(cls.source)],
            check=True, timeout=30,
        )

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.output = Path(self.workspace.name) / "clips"
        self.ranges = [
            ("00:00:00.200", "00:00:01.200"),
            ("00:00:01.500", "00:00:02.500"),
        ]
        self.manifest = clip(self.source, self.output, self.ranges)

    # -- build_bundle ----------------------------------------------------

    def test_bundle_carries_required_metadata(self):
        bundle = build_bundle(
            task_id="task-001",
            source_path=self.source,
            output_dir=self.output,
            ranges=self.ranges,
            source_attribution="synthetic testsrc2 fixture",
        )
        validate_bundle(bundle)
        self.assertEqual(bundle["bundle_schema_version"], 1)
        self.assertEqual(bundle["task_id"], "task-001")
        self.assertEqual(bundle["core_version"], CORE_VERSION)
        self.assertEqual(bundle["source"]["filename"], "source.mp4")
        self.assertEqual(bundle["source"]["bytes"], self.source.stat().st_size)
        self.assertEqual(len(bundle["source"]["sha256"]), 64)
        self.assertEqual(bundle["source"]["attribution"],
                         "synthetic testsrc2 fixture")
        self.assertEqual(len(bundle["bundle_id"]), 64)
        self.assertEqual([c["clip_id"] for c in bundle["clips"]],
                         ["clip-001", "clip-002"])
        for entry, manifest_entry, rng in zip(
                bundle["clips"], self.manifest["clips"], self.ranges):
            self.assertEqual(entry["filename"], manifest_entry["filename"])
            self.assertEqual(entry["bytes"], manifest_entry["bytes"])
            self.assertEqual(entry["sha256"], manifest_entry["sha256"])
            self.assertEqual(entry["range"], {"start": rng[0], "end": rng[1]})
            self.assertEqual(entry["captions"],
                             {"available": False, "filename": None})
        self.assertEqual(bundle["model_scores"], [])

    def test_bundle_id_is_deterministic_across_runs(self):
        b1 = build_bundle(task_id="task-x", source_path=self.source,
                          output_dir=self.output)
        b2 = build_bundle(task_id="task-x", source_path=self.source,
                          output_dir=self.output)
        self.assertEqual(b1["bundle_id"], b2["bundle_id"])
        b3 = build_bundle(task_id="task-y", source_path=self.source,
                          output_dir=self.output)
        self.assertNotEqual(b1["bundle_id"], b3["bundle_id"])

    def test_bundle_detects_caption_sidecars(self):
        sidecar = self.output / "clip-001.vtt"
        sidecar.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhi\n",
                           encoding="utf-8")
        bundle = build_bundle(task_id="t", source_path=self.source,
                              output_dir=self.output)
        self.assertEqual(bundle["clips"][0]["captions"],
                         {"available": True, "filename": "clip-001.vtt"})
        self.assertEqual(bundle["clips"][1]["captions"],
                         {"available": False, "filename": None})

    def test_bundle_rejects_manifest_hash_mismatch(self):
        # Truncate a rendered clip so its SHA no longer matches the manifest.
        target = self.output / self.manifest["clips"][0]["filename"]
        with target.open("r+b") as fh:
            fh.truncate(max(1, target.stat().st_size - 1))
        with self.assertRaisesRegex(BundleError, "hash/size"):
            build_bundle(task_id="t", source_path=self.source,
                         output_dir=self.output)

    def test_bundle_rejects_bad_inputs(self):
        for kwargs, pattern in [
            ({"task_id": "bad id!"}, "task_id"),
            ({"task_id": ""}, "task_id"),
            ({"source_path": self.source.parent / "missing.mp4"}, "source_path"),
            ({"output_dir": self.output.parent / "missing"}, "output_dir"),
        ]:
            base = dict(task_id="task", source_path=self.source,
                        output_dir=self.output)
            base.update(kwargs)
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(BundleError, pattern):
                    build_bundle(**base)

    def test_model_scores_carry_provenance_and_not_approval(self):
        bundle = build_bundle(
            task_id="task", source_path=self.source, output_dir=self.output,
            model_scores=[
                {"clip_id": "clip-001", "model_name": "highlight-v1",
                 "model_version": "0.1.0", "produced_at": "2026-09-30T00:00:00Z",
                 "score": 0.75},
            ],
        )
        validate_bundle(bundle)
        (score,) = bundle["model_scores"]
        self.assertEqual(score["model_name"], "highlight-v1")
        self.assertEqual(score["model_version"], "0.1.0")
        self.assertEqual(score["produced_at"], "2026-09-30T00:00:00Z")
        self.assertEqual(score["score"], 0.75)
        self.assertIsNone(score["approval"])

    def test_model_scores_reject_bad_data(self):
        common = dict(task_id="t", source_path=self.source,
                      output_dir=self.output)
        cases = [
            [{"clip_id": "clip-001", "model_name": "m", "model_version": "1",
              "produced_at": "now", "score": "high"}],
            [{"clip_id": "clip-001", "model_name": "m", "model_version": "1",
              "produced_at": "now", "score": 1.0},
             {"clip_id": "clip-001", "model_name": "m", "model_version": "1",
              "produced_at": "later", "score": 2.0}],
            [{"clip_id": "clip-999", "model_name": "m", "model_version": "1",
              "produced_at": "now", "score": 1.0}],
        ]
        for scores in cases:
            with self.subTest(scores=scores):
                with self.assertRaises(BundleError):
                    build_bundle(model_scores=scores, **common)

    # -- validate_bundle -------------------------------------------------

    def test_validate_bundle_rejects_wire_approval_field_set(self):
        bundle = build_bundle(
            task_id="t", source_path=self.source, output_dir=self.output,
            model_scores=[
                {"clip_id": "clip-001", "model_name": "m",
                 "model_version": "1", "produced_at": "now", "score": 1.0},
            ],
        )
        # A malicious/broken producer that puts approval on a model score.
        tampered = copy.deepcopy(bundle)
        tampered["model_scores"][0]["approval"] = True
        with self.assertRaisesRegex(BundleError, "approval"):
            validate_bundle(tampered)

    # -- MockConsumer ----------------------------------------------------

    def _bundle(self, task_id="task-001", **kwargs):
        return build_bundle(task_id=task_id, source_path=self.source,
                            output_dir=self.output, **kwargs)

    def test_consumer_accepts_then_reports_duplicate(self):
        consumer = MockConsumer()
        bundle = self._bundle()
        self.assertEqual(consumer.import_bundle(bundle, self.output),
                         "accepted")
        # Same payload -> duplicate, state unchanged.
        self.assertEqual(consumer.import_bundle(bundle, self.output),
                         "duplicate")
        self.assertEqual(len(consumer.bundles), 1)

    def test_consumer_rejects_corrupt_bundle_json(self):
        consumer = MockConsumer()
        broken = self._bundle()
        broken["clips"][0]["sha256"] = "not-a-hash"
        with self.assertRaisesRegex(ConsumerError, "corrupt"):
            consumer.import_bundle(broken, self.output)
        self.assertEqual(consumer.bundles, {})

    def test_consumer_rejects_corrupt_clip_file(self):
        consumer = MockConsumer()
        bundle = self._bundle()
        target = self.output / bundle["clips"][0]["filename"]
        # Flip a byte in the middle so size matches but hash does not.
        with target.open("r+b") as fh:
            data = bytearray(fh.read())
            data[len(data) // 2] ^= 0xFF
            fh.seek(0)
            fh.write(bytes(data))
        with self.assertRaisesRegex(ConsumerError, "corrupt"):
            consumer.import_bundle(bundle, self.output)
        self.assertEqual(consumer.bundles, {})

    def test_consumer_rejects_partial_bundle_missing_clip(self):
        consumer = MockConsumer()
        bundle = self._bundle()
        (self.output / bundle["clips"][1]["filename"]).unlink()
        with self.assertRaisesRegex(ConsumerError, "partial"):
            consumer.import_bundle(bundle, self.output)
        self.assertEqual(consumer.bundles, {})

    def test_consumer_accepts_out_of_order_bundles(self):
        # Second output directory for a different task, imported first.
        second_out = Path(self.workspace.name) / "clips2"
        clip(self.source, second_out, [("00:00:00", "00:00:01")])
        consumer = MockConsumer()
        later = build_bundle(task_id="task-late", source_path=self.source,
                             output_dir=second_out)
        earlier = self._bundle(task_id="task-early")
        self.assertEqual(consumer.import_bundle(later, second_out), "accepted")
        self.assertEqual(consumer.import_bundle(earlier, self.output),
                         "accepted")
        self.assertEqual({b["task_id"] for b in consumer.bundles.values()},
                         {"task-early", "task-late"})

    def test_repeated_import_preserves_human_ratings(self):
        consumer = MockConsumer()
        bundle = self._bundle()
        consumer.import_bundle(bundle, self.output)
        consumer.set_rating(bundle["bundle_id"], "clip-001", 4, note="keeper")
        consumer.set_rating(bundle["bundle_id"], "clip-002", 2)
        # Re-import identical bundle: must be a no-op for ratings.
        self.assertEqual(consumer.import_bundle(bundle, self.output),
                         "duplicate")
        self.assertEqual(consumer.get_rating(bundle["bundle_id"], "clip-001"),
                         {"rating": 4, "note": "keeper"})
        self.assertEqual(consumer.get_rating(bundle["bundle_id"], "clip-002"),
                         {"rating": 2, "note": ""})
        # Serialising and re-importing after a round-trip through JSON also
        # counts as a repeat: ratings still there.
        round_tripped = json.loads(json.dumps(bundle))
        self.assertEqual(consumer.import_bundle(round_tripped, self.output),
                         "duplicate")
        self.assertEqual(consumer.get_rating(bundle["bundle_id"], "clip-001"),
                         {"rating": 4, "note": "keeper"})

    def test_bundle_id_collision_with_different_payload_is_rejected(self):
        consumer = MockConsumer()
        bundle = self._bundle()
        consumer.import_bundle(bundle, self.output)
        tampered = copy.deepcopy(bundle)
        tampered["source"]["attribution"] = "changed"
        with self.assertRaisesRegex(ConsumerError, "collision"):
            consumer.import_bundle(tampered, self.output)

    def test_ratings_reject_unknown_ids_and_out_of_range(self):
        consumer = MockConsumer()
        bundle = self._bundle()
        consumer.import_bundle(bundle, self.output)
        bid = bundle["bundle_id"]
        with self.assertRaises(ConsumerError):
            consumer.set_rating("no-such-bundle", "clip-001", 3)
        with self.assertRaises(ConsumerError):
            consumer.set_rating(bid, "clip-999", 3)
        with self.assertRaises(ConsumerError):
            consumer.set_rating(bid, "clip-001", 99)
        with self.assertRaises(ConsumerError):
            consumer.set_rating(bid, "clip-001", True)  # bool is not int here

    def test_consumer_skips_file_check_when_clip_dir_absent(self):
        # A downstream consumer may receive a bundle out of band from its
        # files. Contract validation still runs.
        consumer = MockConsumer()
        bundle = self._bundle()
        self.assertEqual(consumer.import_bundle(bundle), "accepted")


if __name__ == "__main__":
    unittest.main()
