"""Local, bounded FFmpeg clip rendering."""

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path


CORE_VERSION = "1.0.0"
MAX_BYTES = 100_000_000
MAX_DURATION = Decimal("120")
MAX_CLIPS = 3
TIME_LIMIT = 840  # Shared budget for all probes and renders, below 15 minutes.
TIMESTAMP = re.compile(r"^(\d+):([0-5]\d):([0-5]\d)(?:\.(\d{1,6}))?$")


class ClipError(ValueError):
    """Invalid input or failed rendering."""


def _sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_path(value, *, existing):
    path = Path(value).expanduser()
    if ".." in path.parts:
        raise ClipError("path traversal is not allowed")
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ClipError("symlink paths are not allowed")
    if existing:
        if not path.is_file():
            raise ClipError("source must be an existing local regular file")
    elif path.exists():
        raise ClipError("output directory already exists")
    elif not path.parent.is_dir():
        raise ClipError("output parent directory must exist")
    return path


def _run(args, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ClipError("FFmpeg job timed out")
    try:
        process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
        try:
            stdout, _ = process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise ClipError("FFmpeg job timed out") from None
    except FileNotFoundError as exc:
        raise ClipError(f"required executable not found: {args[0]}") from exc
    if process.returncode:
        raise ClipError(f"{args[0]} failed (exit {process.returncode})")
    return stdout


def _duration(path, deadline):
    output = _run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        deadline,
    )
    try:
        duration = Decimal(str(json.loads(output)["format"]["duration"]))
    except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
        raise ClipError("video has no valid duration") from exc
    if not duration.is_finite() or duration <= 0:
        raise ClipError("video has no valid duration")
    return duration


def _timestamp(value):
    match = TIMESTAMP.fullmatch(value)
    if not match:
        raise ClipError("timestamps must be HH:MM:SS[.ffffff]")
    hours, minutes, seconds, fraction = match.groups()
    return (Decimal(hours) * 3600 + Decimal(minutes) * 60
            + Decimal(seconds) + Decimal("0." + fraction if fraction else "0"))


def _build_result_bundle(source, clips, parsed, *, task_id=None, source_provenance="local-file", model_scores=None):
    if task_id is None:
        task_id = f"task-{int(time.time() * 1000)}-{os.getpid()}"
    if not task_id:
        raise ClipError("task_id must be non-empty")
    if not source_provenance:
        raise ClipError("source provenance must be non-empty")
    source_hash = _sha256_file(source)
    bundle_clips = []
    for index, clip_info in enumerate(clips):
        start, end = parsed[index]
        opaque_id = hashlib.sha256(
            f"{task_id}:{index + 1}:{clip_info['sha256']}".encode("utf-8")
        ).hexdigest()[:24]
        bundle_clips.append({
            "clip_id": opaque_id,
            "filename": clip_info["filename"],
            "bytes": clip_info["bytes"],
            "sha256": clip_info["sha256"],
            "duration_seconds": clip_info["duration_seconds"],
            "requested_range": {"start": str(start), "end": str(end)},
            "captions": {"available": False},
        })
    bundle = {
        "schema_version": 3,
        "task_id": task_id,
        "core_version": CORE_VERSION,
        "source": {
            "provenance": source_provenance,
            "sha256": source_hash,
            "bytes": source.stat().st_size,
        },
        "clips": bundle_clips,
    }
    if model_scores is not None:
        provenance = model_scores.get("provenance")
        scores = model_scores.get("scores")
        if not isinstance(provenance, str) or not provenance:
            raise ClipError("model score provenance must be non-empty")
        if not isinstance(scores, list):
            raise ClipError("model scores must be a list")
        bundle["model_scores"] = {
            "provenance": provenance,
            "scores": scores,
        }
    return bundle


def _safe_bundle_relative_path(bundle_dir, filename):
    relpath = Path(filename)
    if relpath.is_absolute() or ".." in relpath.parts:
        raise ClipError("bundle clip filename must be a safe relative path")
    target = (bundle_dir / relpath).resolve()
    root = bundle_dir.resolve()
    if os.path.commonpath([str(root), str(target)]) != str(root):
        raise ClipError("bundle clip filename escapes bundle directory")
    return target


class MockBundleConsumer:
    """Local-only bundle importer for private consumers."""

    def __init__(self, state_path):
        state_path = Path(state_path).expanduser()
        if ".." in state_path.parts:
            raise ClipError("path traversal is not allowed")
        self.state_path = Path(os.path.abspath(state_path))
        for part in (self.state_path, *self.state_path.parents):
            if part.is_symlink():
                raise ClipError("symlink paths are not allowed")
        if not self.state_path.parent.is_dir():
            raise ClipError("state parent directory must exist")

    def _read_state(self):
        if not self.state_path.exists():
            return {"schema_version": 1, "imports": {}, "human_ratings": {}}
        try:
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ClipError("consumer state is not valid JSON") from exc
        if not isinstance(loaded, dict):
            raise ClipError("consumer state must be an object")
        imports = loaded.get("imports")
        human_ratings = loaded.get("human_ratings")
        if not isinstance(imports, dict) or not isinstance(human_ratings, dict):
            raise ClipError("consumer state has invalid top-level fields")
        return {
            "schema_version": 1,
            "imports": imports,
            "human_ratings": human_ratings,
        }

    def _write_state(self, state):
        temporary = self.state_path.with_name(self.state_path.name + ".tmp")
        temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.state_path)

    def import_bundle(self, bundle_path):
        bundle_path = _safe_path(bundle_path, existing=True)
        bundle_dir = bundle_path.parent
        try:
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ClipError("bundle is not valid JSON") from exc
        if bundle.get("schema_version") != 3:
            raise ClipError("unsupported bundle schema version")
        task_id = bundle.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ClipError("bundle task_id must be non-empty")
        source = bundle.get("source")
        if not isinstance(source, dict):
            raise ClipError("bundle source metadata is required")
        for field in ("provenance", "sha256", "bytes"):
            if field not in source:
                raise ClipError("bundle source metadata is incomplete")
        clips = bundle.get("clips")
        if not isinstance(clips, list) or not clips:
            raise ClipError("bundle clips must be a non-empty list")

        normalized = {}
        for clip_entry in clips:
            if not isinstance(clip_entry, dict):
                raise ClipError("bundle clip entry must be an object")
            for field in (
                "clip_id", "filename", "bytes", "sha256",
                "duration_seconds", "requested_range", "captions",
            ):
                if field not in clip_entry:
                    raise ClipError("bundle clip entry is incomplete")
            clip_id = clip_entry["clip_id"]
            if clip_id in normalized:
                raise ClipError("bundle contains duplicate clip IDs")
            clip_file = _safe_bundle_relative_path(bundle_dir, clip_entry["filename"])
            if not clip_file.is_file():
                raise ClipError("bundle clip file is missing")
            byte_length = clip_file.stat().st_size
            if byte_length != clip_entry["bytes"]:
                raise ClipError("bundle clip byte length mismatch")
            digest = _sha256_file(clip_file)
            if digest != clip_entry["sha256"]:
                raise ClipError("bundle clip sha256 mismatch")
            normalized[clip_id] = {
                "filename": clip_entry["filename"],
                "bytes": clip_entry["bytes"],
                "sha256": clip_entry["sha256"],
                "duration_seconds": clip_entry["duration_seconds"],
                "requested_range": clip_entry["requested_range"],
                "captions": clip_entry["captions"],
            }

        if "model_scores" in bundle:
            scores = bundle["model_scores"]
            if not isinstance(scores, dict):
                raise ClipError("model_scores must be an object")
            if not isinstance(scores.get("provenance"), str) or not scores["provenance"]:
                raise ClipError("model_scores provenance must be non-empty")
            if not isinstance(scores.get("scores"), list):
                raise ClipError("model_scores scores must be a list")

        state = self._read_state()
        imports = state["imports"]
        bundle_hash = hashlib.sha256(
            json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if task_id in imports:
            if imports[task_id]["bundle_sha256"] != bundle_hash:
                raise ClipError("duplicate task_id has mismatched bundle content")
            return {"status": "duplicate_ignored", "task_id": task_id, "imported_clips": 0}

        imports[task_id] = {
            "bundle_sha256": bundle_hash,
            "core_version": bundle.get("core_version"),
            "source": source,
            "clips": normalized,
            "model_scores": bundle.get("model_scores"),
        }
        self._write_state(state)
        return {"status": "imported", "task_id": task_id, "imported_clips": len(normalized)}

    def set_human_rating(self, clip_id, rating):
        if not isinstance(clip_id, str) or not clip_id:
            raise ClipError("clip_id must be non-empty")
        state = self._read_state()
        state["human_ratings"][clip_id] = rating
        self._write_state(state)

    def get_human_rating(self, clip_id):
        state = self._read_state()
        return state["human_ratings"].get(clip_id)


def clip(source, output_dir, ranges):
    """Render supplied (start, end) timestamp pairs and return the manifest.

    The destination must not exist; it is published only after every clip and
    its metadata are successfully generated.
    """
    source = _safe_path(source, existing=True)
    output_dir = _safe_path(output_dir, existing=False)
    if not 1 <= len(ranges) <= MAX_CLIPS:
        raise ClipError("provide between one and three clips")
    if source.stat().st_size > MAX_BYTES:
        raise ClipError("source exceeds 100 MB")
    deadline = time.monotonic() + TIME_LIMIT
    source_duration = _duration(source, deadline)
    if source_duration > MAX_DURATION:
        raise ClipError("source exceeds two minutes")
    parsed = []
    for start_text, end_text in ranges:
        start, end = _timestamp(start_text), _timestamp(end_text)
        if start < 0 or end <= start or end > source_duration:
            raise ClipError("clip range must be positive and within source duration")
        parsed.append((start, end))

    temporary = Path(tempfile.mkdtemp(prefix=".video-clipper-", dir=output_dir.parent))
    try:
        clips = []
        total = 0
        for index, (start, end) in enumerate(parsed, 1):
            name = f"clip-{index:03d}.mp4"
            rendered = temporary / name
            _run(
                ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(source),
                 "-ss", str(start), "-t", str(end - start),
                 "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "-1",
                 "-map_chapters", "-1", "-c:v", "mpeg4", "-q:v", "4",
                 "-c:a", "aac", "-pix_fmt", "yuv420p", "-threads", "1",
                 "-fs", str(MAX_BYTES - total + 1), str(rendered)],
                deadline,
            )
            size = rendered.stat().st_size
            total += size
            if total > MAX_BYTES:
                raise ClipError("total output exceeds 100 MB")
            actual_duration = _duration(rendered, deadline)
            if abs(actual_duration - (end - start)) > Decimal("0.25"):
                raise ClipError("rendered clip duration differs from requested range")
            digest = hashlib.sha256()
            with rendered.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            clips.append({
                "id": f"clip-{index:03d}",
                "filename": name,
                "bytes": size,
                "sha256": digest.hexdigest(),
                "duration_seconds": float(actual_duration),
            })
        manifest = {"schema_version": 1, "clips": clips}
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        bundle = _build_result_bundle(source, clips, parsed)
        (temporary / "result_bundle.json").write_text(
            json.dumps(bundle, indent=2) + "\n", encoding="utf-8"
        )
        if output_dir.exists() or output_dir.is_symlink():
            raise ClipError("output directory already exists")
        temporary.rename(output_dir)
        return manifest
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Clip a local video using FFmpeg")
    parser.add_argument("source", help="local source video file")
    parser.add_argument("output_dir", help="new output directory")
    parser.add_argument("--clip", nargs=2, action="append", required=True,
                        metavar=("START", "END"), help="HH:MM:SS[.ffffff] timestamps")
    args = parser.parse_args(argv)
    try:
        clip(args.source, args.output_dir, args.clip)
    except (ClipError, OSError) as exc:
        parser.exit(1, f"video-clipper: {exc}\n")


if __name__ == "__main__":
    main()
