import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_clipper import ClipError, MockBundleConsumer, _run, clip


class ClipperTests(unittest.TestCase):
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

    def test_manifest_matches_playable_clips(self):
        result = clip(self.source, self.output, [
            ("00:00:00.200", "00:00:01.200"),
            ("00:00:01.500", "00:00:02.500"),
        ])
        self.assertEqual(result, json.loads((self.output / "manifest.json").read_text()))
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual([item["id"] for item in result["clips"]],
                         ["clip-001", "clip-002"])
        for item in result["clips"]:
            path = self.output / item["filename"]
            self.assertEqual(path.stat().st_size, item["bytes"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item["sha256"])
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "json", str(path)],
                capture_output=True, check=True, timeout=30,
            )
            self.assertAlmostEqual(
                float(json.loads(probe.stdout)["format"]["duration"]),
                item["duration_seconds"], places=3,
            )
            self.assertLessEqual(abs(item["duration_seconds"] - 1), 0.25)
        bundle = json.loads((self.output / "result_bundle.json").read_text())
        self.assertEqual(bundle["schema_version"], 3)
        self.assertTrue(bundle["task_id"])
        self.assertEqual(bundle["core_version"], "1.0.0")
        self.assertEqual(bundle["source"]["provenance"], "local-file")
        self.assertEqual(bundle["source"]["bytes"], self.source.stat().st_size)
        self.assertEqual(bundle["source"]["sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(len(bundle["clips"]), 2)
        self.assertIn("clip_id", bundle["clips"][0])
        self.assertTrue(bundle["clips"][0]["captions"]["available"] is False)
        self.assertEqual(bundle["clips"][0]["requested_range"]["start"], "0.2")
        self.assertEqual(bundle["clips"][0]["requested_range"]["end"], "1.2")

    def test_rejects_invalid_ranges_without_output(self):
        for ranges in [
            [], [("00:00:00", "00:00:01")] * 4,
            [("00:00:02", "00:00:01")],
            [("00:00:00", "00:00:04")],
            [("NaN", "00:00:01")],
        ]:
            with self.subTest(ranges=ranges):
                with self.assertRaises(ClipError):
                    clip(self.source, self.output, ranges)
                self.assertFalse(self.output.exists())

    def test_rejects_symlinks_traversal_and_existing_output(self):
        symlink = Path(self.workspace.name) / "linked.mp4"
        symlink.symlink_to(self.source)
        for source in [symlink, self.source.parent / ".." / self.source.parent.name / self.source.name]:
            with self.assertRaises(ClipError):
                clip(source, self.output, [("00:00:00", "00:00:01")])
        linked_parent = Path(self.workspace.name) / "linked-parent"
        linked_parent.symlink_to(Path(self.workspace.name), target_is_directory=True)
        with self.assertRaises(ClipError):
            clip(self.source, linked_parent / "clips", [("00:00:00", "00:00:01")])
        self.output.mkdir()
        with self.assertRaises(ClipError):
            clip(self.source, self.output, [("00:00:00", "00:00:01")])

    def test_rejects_oversized_source_and_duration(self):
        with patch("video_clipper.MAX_BYTES", 1):
            with self.assertRaisesRegex(ClipError, "100 MB"):
                clip(self.source, self.output, [("00:00:00", "00:00:01")])
        with patch("video_clipper.MAX_DURATION", 1):
            with self.assertRaisesRegex(ClipError, "two minutes"):
                clip(self.source, self.output, [("00:00:00", "00:00:01")])
        self.assertFalse(self.output.exists())

    def test_rejects_oversized_total_output_without_partial_result(self):
        with patch("video_clipper.MAX_BYTES", self.source.stat().st_size + 1):
            with self.assertRaises(ClipError):
                clip(self.source, self.output, [("00:00:00", "00:00:02")] * 3)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(Path(self.workspace.name).iterdir()), [])

    def test_rejects_invalid_video(self):
        invalid = Path(self.workspace.name) / "invalid.mp4"
        invalid.write_bytes(b"not video")
        with self.assertRaises(ClipError):
            clip(invalid, self.output, [("00:00:00", "00:00:01")])
        self.assertFalse(self.output.exists())

    def test_render_failure_rolls_back_all_clips(self):
        import video_clipper
        original = video_clipper._run

        def fail_second(args, deadline):
            if args[0] == "ffmpeg" and "clip-002.mp4" in args[-1]:
                raise ClipError("render failed")
            return original(args, deadline)

        with patch("video_clipper._run", side_effect=fail_second):
            with self.assertRaisesRegex(ClipError, "render failed"):
                clip(self.source, self.output, [
                    ("00:00:00", "00:00:01"), ("00:00:01", "00:00:02"),
                ])
        self.assertFalse(self.output.exists())
        self.assertEqual(list(Path(self.workspace.name).iterdir()), [])

    def test_hanging_subprocess_times_out(self):
        import time
        with self.assertRaisesRegex(ClipError, "timed out"):
            _run(["python3", "-c", "import time; time.sleep(10)"],
                 time.monotonic() + 0.05)

    def test_mock_consumer_out_of_order_duplicate_is_idempotent(self):
        clip(self.source, self.output, [
            ("00:00:00.200", "00:00:01.200"),
            ("00:00:01.500", "00:00:02.500"),
        ])
        bundle_path = self.output / "result_bundle.json"
        bundle = json.loads(bundle_path.read_text())
        bundle["clips"] = list(reversed(bundle["clips"]))
        bundle_path.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")

        consumer = MockBundleConsumer(Path(self.workspace.name) / "consumer-state.json")
        imported = consumer.import_bundle(bundle_path)
        self.assertEqual(imported["status"], "imported")
        rated_clip = bundle["clips"][0]["clip_id"]
        consumer.set_human_rating(rated_clip, 4.5)
        self.assertEqual(consumer.get_human_rating(rated_clip), 4.5)

        duplicate = consumer.import_bundle(bundle_path)
        self.assertEqual(duplicate["status"], "duplicate_ignored")
        self.assertEqual(consumer.get_human_rating(rated_clip), 4.5)

    def test_mock_consumer_rejects_partial_bundle(self):
        clip(self.source, self.output, [("00:00:00.200", "00:00:01.200")])
        bundle_path = self.output / "result_bundle.json"
        bundle = json.loads(bundle_path.read_text())
        (self.output / bundle["clips"][0]["filename"]).unlink()
        consumer = MockBundleConsumer(Path(self.workspace.name) / "consumer-state.json")
        with self.assertRaisesRegex(ClipError, "missing"):
            consumer.import_bundle(bundle_path)

    def test_mock_consumer_rejects_corrupt_bundle(self):
        clip(self.source, self.output, [("00:00:00.200", "00:00:01.200")])
        bundle_path = self.output / "result_bundle.json"
        bundle = json.loads(bundle_path.read_text())
        clip_file = self.output / bundle["clips"][0]["filename"]
        clip_file.write_bytes(clip_file.read_bytes() + b"corrupt")
        consumer = MockBundleConsumer(Path(self.workspace.name) / "consumer-state.json")
        with self.assertRaisesRegex(ClipError, "mismatch"):
            consumer.import_bundle(bundle_path)

    def test_mock_consumer_rejects_duplicate_task_with_different_content(self):
        clip(self.source, self.output, [("00:00:00.200", "00:00:01.200")])
        bundle_path = self.output / "result_bundle.json"
        bundle = json.loads(bundle_path.read_text())
        consumer = MockBundleConsumer(Path(self.workspace.name) / "consumer-state.json")
        self.assertEqual(consumer.import_bundle(bundle_path)["status"], "imported")
        bundle["clips"][0]["captions"]["available"] = True
        bundle_path.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ClipError, "duplicate task_id"):
            consumer.import_bundle(bundle_path)


if __name__ == "__main__":
    unittest.main()
