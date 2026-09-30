import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mock_consumer import BundleImportError, MockBundleConsumer
from video_clipper import ClipError, _run, clip


HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg/ffprobe not available")
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

    def test_result_bundle_matches_playable_clips(self):
        result = clip(self.source, self.output, [
            ("00:00:00.200", "00:00:01.200"),
            ("00:00:01.500", "00:00:02.500"),
        ], task_id="task-test-1")
        self.assertEqual(
            result, json.loads((self.output / "result_bundle.json").read_text(encoding="utf-8"))
        )
        self.assertEqual(result["schema_version"], 3)
        self.assertEqual(result["task_id"], "task-test-1")
        self.assertEqual(result["source"]["filename"], self.source.name)
        self.assertEqual(result["source"]["bytes"], self.source.stat().st_size)
        self.assertEqual(
            result["source"]["sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest()
        )
        self.assertEqual(
            [item["id"] for item in result["clips"]], ["clip-001", "clip-002"]
        )
        self.assertEqual(
            [item["filename"] for item in result["clips"]], ["clip-001.mp4", "clip-002.mp4"]
        )
        self.assertEqual(
            [item["requested_range_seconds"] for item in result["clips"]],
            [
                {"start_seconds": 0.2, "end_seconds": 1.2},
                {"start_seconds": 1.5, "end_seconds": 2.5},
            ],
        )
        for item in result["clips"]:
            self.assertRegex(item["clip_id"], r"^clip_[0-9a-f]{16}$")
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
            self.assertEqual(item["captions"], {"available": False, "filename": None})
        legacy = json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(legacy["schema_version"], 1)

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


class MockConsumerTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.consumer = MockBundleConsumer(self.root / "consumer-state.json")

    def _make_bundle(self, bundle_dir, task_id, clips):
        bundle_dir.mkdir(parents=True, exist_ok=False)
        clip_items = []
        for index, payload in enumerate(clips, 1):
            filename = f"clip-{index:03d}.mp4"
            path = bundle_dir / filename
            path.write_bytes(payload)
            clip_items.append({
                "clip_id": f"clip_{index:016x}",
                "id": f"clip-{index:03d}",
                "filename": filename,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "duration_seconds": 1.0,
                "requested_range_seconds": {
                    "start_seconds": float(index - 1),
                    "end_seconds": float(index),
                },
                "captions": {"available": False, "filename": None},
            })
        bundle = {
            "schema_version": 3,
            "task_id": task_id,
            "core_version": "1.0.0",
            "source": {
                "filename": "source.mp4",
                "bytes": 1234,
                "sha256": hashlib.sha256(b"source").hexdigest(),
                "provenance": {"kind": "synthetic_fixture"},
            },
            "clips": clip_items,
        }
        (bundle_dir / "result_bundle.json").write_text(
            json.dumps(bundle, indent=2) + "\n", encoding="utf-8"
        )
        return bundle

    def test_import_is_idempotent_and_preserves_human_ratings(self):
        bundle_dir = self.root / "task-a"
        result = self._make_bundle(bundle_dir, "task-a", [b"aaa"])
        first = self.consumer.import_bundle(bundle_dir)
        self.assertTrue(first["new_task"])
        clip_id = result["clips"][0]["clip_id"]
        self.consumer.set_human_rating("task-a", clip_id, 4.5)

        second = self.consumer.import_bundle(bundle_dir)
        self.assertFalse(second["new_task"])
        state = self.consumer.get_state()
        self.assertEqual(state["task_order"], ["task-a"])
        self.assertEqual(state["tasks"]["task-a"]["clips"][clip_id]["human_rating"], 4.5)

    def test_rejects_corrupt_and_partial_bundles_without_mutating_state(self):
        valid_dir = self.root / "task-valid"
        valid = self._make_bundle(valid_dir, "task-valid", [b"bbb"])
        self.consumer.import_bundle(valid_dir)
        before = json.dumps(self.consumer.get_state(), sort_keys=True)

        corrupt_dir = self.root / "task-corrupt"
        shutil.copytree(valid_dir, corrupt_dir)
        corrupt_bundle_path = corrupt_dir / "result_bundle.json"
        corrupt_bundle = json.loads(corrupt_bundle_path.read_text(encoding="utf-8"))
        corrupt_bundle["clips"][0]["sha256"] = "0" * 64
        corrupt_bundle_path.write_text(json.dumps(corrupt_bundle, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(BundleImportError, "hash mismatch"):
            self.consumer.import_bundle(corrupt_dir)

        partial_dir = self.root / "task-partial"
        shutil.copytree(valid_dir, partial_dir)
        (partial_dir / valid["clips"][0]["filename"]).unlink()
        with self.assertRaisesRegex(BundleImportError, "missing clip file"):
            self.consumer.import_bundle(partial_dir)

        after = json.dumps(self.consumer.get_state(), sort_keys=True)
        self.assertEqual(before, after)

    def test_accepts_out_of_order_task_imports_and_duplicate_task_reimports(self):
        task_a = self.root / "task-a"
        task_b = self.root / "task-b"
        self._make_bundle(task_a, "task-a", [b"task-a"])
        self._make_bundle(task_b, "task-b", [b"task-b"])

        self.consumer.import_bundle(task_b)
        self.consumer.import_bundle(task_a)
        self.consumer.import_bundle(task_b)
        state = self.consumer.get_state()
        self.assertEqual(state["task_order"], ["task-b", "task-a"])
        self.assertEqual(sorted(state["tasks"].keys()), ["task-a", "task-b"])

    def test_accepts_clip_entries_in_out_of_order_sequence(self):
        bundle_dir = self.root / "task-order"
        self._make_bundle(bundle_dir, "task-order", [b"1", b"2"])
        path = bundle_dir / "result_bundle.json"
        bundle = json.loads(path.read_text(encoding="utf-8"))
        bundle["clips"] = list(reversed(bundle["clips"]))
        path.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")

        self.consumer.import_bundle(bundle_dir)
        state = self.consumer.get_state()
        self.assertEqual(
            state["tasks"]["task-order"]["clip_order"],
            [item["clip_id"] for item in bundle["clips"]],
        )


if __name__ == "__main__":
    unittest.main()
