"""Local-only mock bundle consumer with idempotent imports."""

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path


class BundleImportError(ValueError):
    """Invalid or incompatible bundle content."""


def _sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleImportError(f"invalid JSON: {path.name}") from exc


class MockBundleConsumer:
    """Filesystem-backed test consumer for result bundles."""

    def __init__(self, state_path):
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

    def _load_state(self):
        if not self.state_path.exists():
            return {"schema_version": 1, "task_order": [], "tasks": {}}
        state = _read_json(self.state_path)
        if not isinstance(state, dict):
            raise BundleImportError("consumer state is invalid")
        if "tasks" not in state or not isinstance(state["tasks"], dict):
            raise BundleImportError("consumer state is invalid")
        if "task_order" not in state or not isinstance(state["task_order"], list):
            raise BundleImportError("consumer state is invalid")
        return state

    def _save_state(self, state):
        tmp_dir = self.state_path.parent
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", delete=False, dir=tmp_dir, prefix=".mock-consumer-"
        ) as stream:
            json.dump(state, stream, indent=2)
            stream.write("\n")
            tmp_path = Path(stream.name)
        os.replace(tmp_path, self.state_path)

    def _validate_bundle(self, bundle_dir):
        bundle_path = bundle_dir / "result_bundle.json"
        if not bundle_path.is_file():
            raise BundleImportError("missing result_bundle.json")
        bundle = _read_json(bundle_path)
        if not isinstance(bundle, dict):
            raise BundleImportError("bundle must be an object")
        if bundle.get("schema_version") != 3:
            raise BundleImportError("unsupported bundle schema_version")
        if not isinstance(bundle.get("task_id"), str) or not bundle["task_id"].strip():
            raise BundleImportError("bundle task_id is required")
        if not isinstance(bundle.get("core_version"), str) or not bundle["core_version"].strip():
            raise BundleImportError("bundle core_version is required")
        source = bundle.get("source")
        if not isinstance(source, dict):
            raise BundleImportError("bundle source is required")
        for key in ("filename", "sha256", "provenance"):
            if key not in source:
                raise BundleImportError(f"bundle source.{key} is required")
        clips = bundle.get("clips")
        if not isinstance(clips, list) or not clips:
            raise BundleImportError("bundle clips must be a non-empty list")

        seen_clip_ids = set()
        normalized_clips = []
        for clip in clips:
            if not isinstance(clip, dict):
                raise BundleImportError("bundle clip must be an object")
            clip_id = clip.get("clip_id")
            filename = clip.get("filename")
            if not isinstance(clip_id, str) or not clip_id.strip():
                raise BundleImportError("clip clip_id is required")
            if clip_id in seen_clip_ids:
                raise BundleImportError("duplicate clip_id in bundle")
            seen_clip_ids.add(clip_id)
            if not isinstance(filename, str) or not filename.strip():
                raise BundleImportError("clip filename is required")
            if Path(filename).is_absolute() or ".." in Path(filename).parts:
                raise BundleImportError("clip filename must be relative")

            expected_bytes = clip.get("bytes")
            expected_sha = clip.get("sha256")
            if not isinstance(expected_bytes, int) or expected_bytes < 0:
                raise BundleImportError("clip bytes must be a non-negative integer")
            if not isinstance(expected_sha, str) or len(expected_sha) != 64:
                raise BundleImportError("clip sha256 must be a 64-char hex string")

            clip_path = bundle_dir / filename
            if not clip_path.is_file():
                raise BundleImportError(f"missing clip file: {filename}")
            actual_bytes = clip_path.stat().st_size
            if actual_bytes != expected_bytes:
                raise BundleImportError(f"clip size mismatch for {filename}")
            if _sha256_file(clip_path) != expected_sha:
                raise BundleImportError(f"clip hash mismatch for {filename}")

            duration = clip.get("duration_seconds")
            if not isinstance(duration, (int, float)) or not math.isfinite(float(duration)):
                raise BundleImportError("clip duration_seconds must be finite")

            range_info = clip.get("requested_range_seconds")
            if not isinstance(range_info, dict):
                raise BundleImportError("clip requested_range_seconds is required")
            start = range_info.get("start_seconds")
            end = range_info.get("end_seconds")
            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
                raise BundleImportError("clip range must be numeric")
            if float(end) <= float(start):
                raise BundleImportError("clip range end must exceed start")

            captions = clip.get("captions")
            if not isinstance(captions, dict) or not isinstance(captions.get("available"), bool):
                raise BundleImportError("clip captions.available must be a boolean")

            if "model_scores" in clip:
                scores = clip["model_scores"]
                if not isinstance(scores, list):
                    raise BundleImportError("clip model_scores must be a list")
                for score in scores:
                    if not isinstance(score, dict):
                        raise BundleImportError("model score must be an object")
                    if "provenance" not in score or not isinstance(score["provenance"], dict):
                        raise BundleImportError("model score provenance is required")

            normalized_clips.append(clip)

        return bundle

    def import_bundle(self, bundle_dir):
        bundle_dir = Path(bundle_dir)
        bundle = self._validate_bundle(bundle_dir)
        state = self._load_state()
        task_id = bundle["task_id"]
        existing = state["tasks"].get(task_id)

        clip_records = {}
        for item in bundle["clips"]:
            record = dict(item)
            previous_rating = None
            if existing:
                previous = existing.get("clips", {}).get(item["clip_id"])
                if isinstance(previous, dict) and "human_rating" in previous:
                    previous_rating = previous["human_rating"]
            if previous_rating is not None:
                record["human_rating"] = previous_rating
            clip_records[item["clip_id"]] = record

        state["tasks"][task_id] = {
            "task_id": task_id,
            "core_version": bundle["core_version"],
            "source": bundle["source"],
            "clip_order": [item["clip_id"] for item in bundle["clips"]],
            "clips": clip_records,
        }
        if task_id not in state["task_order"]:
            state["task_order"].append(task_id)
        self._save_state(state)
        return {"task_id": task_id, "clip_count": len(bundle["clips"]), "new_task": existing is None}

    def set_human_rating(self, task_id, clip_id, rating):
        if not isinstance(rating, (int, float)) or not math.isfinite(float(rating)):
            raise BundleImportError("human rating must be a finite number")
        state = self._load_state()
        task = state["tasks"].get(task_id)
        if not task:
            raise BundleImportError("unknown task_id")
        clip = task.get("clips", {}).get(clip_id)
        if not clip:
            raise BundleImportError("unknown clip_id")
        clip["human_rating"] = float(rating)
        self._save_state(state)

    def get_state(self):
        return self._load_state()
