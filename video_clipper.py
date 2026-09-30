"""Local, bounded FFmpeg clip rendering."""

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path


MAX_BYTES = 100_000_000
MAX_DURATION = Decimal("120")
MAX_CLIPS = 3
TIME_LIMIT = 840  # Shared budget for all probes and renders, below 15 minutes.
CORE_VERSION = "1.0.0"
TIMESTAMP = re.compile(r"^(\d+):([0-5]\d):([0-5]\d)(?:\.(\d{1,6}))?$")


class ClipError(ValueError):
    """Invalid input or failed rendering."""


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


def _sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_model_scores(value):
    if value is None:
        return None
    if not isinstance(value, list):
        raise ClipError("model_scores must be a list when provided")
    normalized = []
    for item in value:
        if not isinstance(item, dict):
            raise ClipError("each model score must be an object")
        if "provenance" not in item or not isinstance(item["provenance"], dict):
            raise ClipError("each model score must include provenance")
        if "score" not in item or not isinstance(item["score"], (int, float)):
            raise ClipError("each model score must include numeric score")
        score = float(item["score"])
        if not math.isfinite(score):
            raise ClipError("model score must be finite")
        normalized_item = dict(item)
        normalized_item["score"] = score
        normalized.append(normalized_item)
    return normalized


def clip(source, output_dir, ranges, *, task_id=None, source_provenance=None, model_scores=None):
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
    source_size = source.stat().st_size
    source_sha256 = _sha256_file(source)
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
    if task_id is None:
        task_id = f"task-{hashlib.sha256((str(source) + repr(ranges)).encode('utf-8')).hexdigest()[:16]}"
    if not isinstance(task_id, str) or not task_id.strip():
        raise ClipError("task_id must be a non-empty string")
    if source_provenance is None:
        source_provenance = {"kind": "local_file", "descriptor": source.name}
    if not isinstance(source_provenance, dict):
        raise ClipError("source_provenance must be an object")
    if model_scores is not None and len(model_scores) != len(parsed):
        raise ClipError("model_scores must have one entry per requested clip")

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
            rendered_sha256 = _sha256_file(rendered)
            clip_id = "clip_" + hashlib.sha256(
                f"{task_id}:{index}:{name}".encode("utf-8")
            ).hexdigest()[:16]
            item = {
                "clip_id": clip_id,
                "id": f"clip-{index:03d}",
                "filename": name,
                "bytes": size,
                "sha256": rendered_sha256,
                "duration_seconds": float(actual_duration),
                "requested_range_seconds": {
                    "start_seconds": float(start),
                    "end_seconds": float(end),
                },
                "captions": {"available": False, "filename": None},
            }
            if model_scores is not None:
                normalized_scores = _normalize_model_scores(model_scores[index - 1])
                if normalized_scores:
                    item["model_scores"] = normalized_scores
            clips.append(item)
        manifest = {"schema_version": 1, "clips": clips}
        bundle = {
            "schema_version": 3,
            "task_id": task_id,
            "core_version": CORE_VERSION,
            "source": {
                "filename": source.name,
                "bytes": source_size,
                "sha256": source_sha256,
                "provenance": source_provenance,
            },
            "clips": clips,
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        (temporary / "result_bundle.json").write_text(
            json.dumps(bundle, indent=2) + "\n", encoding="utf-8"
        )
        if output_dir.exists() or output_dir.is_symlink():
            raise ClipError("output directory already exists")
        temporary.rename(output_dir)
        return bundle
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
