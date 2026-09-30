import json
import tempfile
import time
import unittest
from pathlib import Path

from transcript import (
    TranscriptError,
    analyze_transcript,
    highlight_candidates,
    load_transcript,
    optional_analysis,
)


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "transcript.json"
        self.path.write_text(json.dumps({"schema_version": 1, "segments": [
            {"start": 0, "end": 1, "text": "A quiet introduction."},
            {"start": 1, "end": 2, "text": "This is amazing!"},
        ]}))
        self.addCleanup(self.directory.cleanup)

    def test_validation_and_deterministic_candidates(self):
        segments = load_transcript(self.path)
        candidates = highlight_candidates(segments, 2)
        self.assertEqual(candidates[0]["id"], "candidate-002")
        self.assertGreater(candidates[0]["suggested_score"], candidates[1]["suggested_score"])
        self.assertIsNone(candidates[0]["human_rating"])

    def test_hash_cache_reuses_result(self):
        first = analyze_transcript(self.path, self.directory.name)
        self.path.touch()
        second = analyze_transcript(self.path, self.directory.name)
        self.assertEqual(first, second)

    def test_rejects_bad_order_and_requires_opt_in(self):
        with self.assertRaises(TranscriptError):
            highlight_candidates([{"start": 1, "end": 2, "text": "x"},
                                  {"start": 0, "end": 1, "text": "y"}])
        with self.assertRaisesRegex(TranscriptError, "opt-in"):
            optional_analysis(lambda source, budget: source, "source")

    def test_mock_adapter_is_bounded_and_explicit(self):
        self.assertEqual(optional_analysis(
            lambda source, budget: (source, budget), "source",
            enabled=True, budget=2), ("source", 2))
        with self.assertRaises(TranscriptError):
            optional_analysis(lambda source, budget: 1 / 0, "source", enabled=True)
        with self.assertRaisesRegex(TranscriptError, "timed out"):
            optional_analysis(lambda source, budget: time.sleep(1), "source",
                              enabled=True, timeout=0.01)


if __name__ == "__main__":
    unittest.main()
