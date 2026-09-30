"""Result bundle contract for V3.

Wraps a V1 clip manifest (produced by :mod:`video_clipper`) with the metadata
required to hand the result off to a downstream consumer:

* task ID and core version
* source provenance (basename, byte length, SHA-256)
* opaque clip IDs, relative filenames, byte lengths, SHA-256, durations, ranges
* caption availability (looks for ``.vtt``/``.srt``/``.captions.json`` sidecars
  next to each rendered clip)
* optional model scores with their own provenance, kept distinct from any
  human approval decision

The bundle format is JSON only. Nothing here performs a network call or
re-renders video; it consumes the on-disk output directory produced by
``video_clipper.clip`` and returns a plain ``dict`` that the caller may
serialise, hand to the mock consumer, or diff in tests.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

BUNDLE_SCHEMA_VERSION = 1
CORE_VERSION = "1.0.0"  # Matches pyproject.toml [project] version at build time.

_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CAPTION_SUFFIXES = (".vtt", ".srt", ".captions.json")


class BundleError(ValueError):
    """Invalid inputs when building or validating a result bundle."""


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _validate_task_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise BundleError("task_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    return task_id


def _caption_for(clip_dir: Path, filename: str) -> dict:
    stem = Path(filename).stem
    for suffix in _CAPTION_SUFFIXES:
        sidecar = clip_dir / f"{stem}{suffix}"
        if sidecar.is_file():
            return {"available": True, "filename": sidecar.name}
    return {"available": False, "filename": None}


def _validate_manifest(manifest: dict) -> list[dict]:
    if not isinstance(manifest, dict):
        raise BundleError("manifest must be an object")
    if manifest.get("schema_version") != 1:
        raise BundleError("unsupported manifest schema_version")
    clips = manifest.get("clips")
    if not isinstance(clips, list) or not clips:
        raise BundleError("manifest.clips must be a non-empty list")
    required = ("id", "filename", "bytes", "sha256", "duration_seconds")
    for entry in clips:
        if not isinstance(entry, dict) or not all(k in entry for k in required):
            raise BundleError("manifest clip entries are missing required fields")
    return clips


def _validate_ranges(clips: list[dict], ranges) -> list[tuple[str, str]]:
    if ranges is None:
        return [("", "")] * len(clips)
    ranges = list(ranges)
    if len(ranges) != len(clips):
        raise BundleError("ranges length must match number of clips")
    parsed: list[tuple[str, str]] = []
    for item in ranges:
        if (not isinstance(item, (list, tuple)) or len(item) != 2
                or not all(isinstance(v, str) for v in item)):
            raise BundleError("each range must be a (start, end) pair of strings")
        parsed.append((item[0], item[1]))
    return parsed


def _validate_model_scores(scores) -> list[dict]:
    if scores is None:
        return []
    if not isinstance(scores, list):
        raise BundleError("model_scores must be a list")
    validated: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in scores:
        if not isinstance(entry, dict):
            raise BundleError("model_scores entries must be objects")
        clip_id = entry.get("clip_id")
        model_name = entry.get("model_name")
        model_version = entry.get("model_version")
        produced_at = entry.get("produced_at")
        score = entry.get("score")
        if not all(isinstance(v, str) and v for v in
                   (clip_id, model_name, model_version, produced_at)):
            raise BundleError("model score requires clip_id, model_name, "
                              "model_version, produced_at strings")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            raise BundleError("model score.score must be numeric")
        key = (clip_id, model_name, model_version)
        if key in seen:
            raise BundleError("duplicate model score for clip+model+version")
        seen.add(key)
        validated.append({
            "clip_id": clip_id,
            "model_name": model_name,
            "model_version": model_version,
            "produced_at": produced_at,
            "score": float(score),
            # Explicitly null: a model score is provenance, not approval.
            "approval": None,
        })
    return validated


def build_bundle(
    *,
    task_id: str,
    source_path,
    output_dir,
    ranges=None,
    source_attribution: str = "",
    model_scores=None,
    core_version: str = CORE_VERSION,
) -> dict:
    """Return a JSON-serialisable result bundle for a completed clipping job.

    Parameters
    ----------
    task_id:
        Caller-supplied opaque identifier for this run. Constrained to a safe
        character set so it can be used as a filename component downstream.
    source_path:
        Path to the local source video that was clipped. Read only to compute
        its SHA-256 and byte length. The absolute path is not stored; only the
        basename is retained as ``source.filename``.
    output_dir:
        Path to the directory produced by ``video_clipper.clip``. Must contain
        a ``manifest.json`` and every clip file it references.
    ranges:
        Optional iterable of ``(start, end)`` timestamp strings, in the same
        order as the manifest's ``clips``. When supplied they are recorded on
        each clip so the consumer can compare requested vs rendered.
    source_attribution:
        Optional free-form provenance note (e.g. "synthetic testsrc2 fixture"
        or a license identifier). Empty by default.
    model_scores:
        Optional list of ``{clip_id, model_name, model_version, produced_at,
        score}`` dicts. Each entry has its ``approval`` field forced to
        ``None`` because model scores are provenance, not human approval.
    core_version:
        Version string embedded in the bundle. Defaults to :data:`CORE_VERSION`.
    """
    task_id = _validate_task_id(task_id)
    if not isinstance(core_version, str) or not core_version:
        raise BundleError("core_version must be a non-empty string")

    source_path = Path(source_path)
    if not source_path.is_file():
        raise BundleError("source_path must be an existing regular file")
    output_dir = Path(output_dir)
    if not output_dir.is_dir():
        raise BundleError("output_dir must be an existing directory")
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.is_file():
        raise BundleError("output_dir is missing manifest.json")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    clips = _validate_manifest(manifest)
    ranges = _validate_ranges(clips, ranges)

    source_sha, source_bytes = _sha256_file(source_path)

    bundle_clips: list[dict] = []
    seen_ids: set[str] = set()
    for entry, (start, end) in zip(clips, ranges):
        clip_id = entry["id"]
        if not isinstance(clip_id, str) or not clip_id:
            raise BundleError("clip id must be a non-empty string")
        if clip_id in seen_ids:
            raise BundleError("duplicate clip id in manifest")
        seen_ids.add(clip_id)
        filename = entry["filename"]
        if not isinstance(filename, str) or "/" in filename or "\\" in filename:
            raise BundleError("clip filename must be a bare relative name")
        clip_path = output_dir / filename
        if not clip_path.is_file():
            raise BundleError(f"clip file missing: {filename}")
        actual_sha, actual_bytes = _sha256_file(clip_path)
        if actual_sha != entry["sha256"] or actual_bytes != entry["bytes"]:
            raise BundleError(f"clip {clip_id} does not match manifest hash/size")
        bundle_clips.append({
            "clip_id": clip_id,
            "filename": filename,
            "bytes": actual_bytes,
            "sha256": actual_sha,
            "duration_seconds": float(entry["duration_seconds"]),
            "range": {"start": start, "end": end},
            "captions": _caption_for(output_dir, filename),
        })

    validated_scores = _validate_model_scores(model_scores)
    known_ids = {c["clip_id"] for c in bundle_clips}
    for score in validated_scores:
        if score["clip_id"] not in known_ids:
            raise BundleError("model score references unknown clip_id")

    # bundle_id is deterministic: same task + same source bytes -> same id.
    # Downstream consumers use this for duplicate detection.
    fingerprint = hashlib.sha256()
    fingerprint.update(task_id.encode("utf-8"))
    fingerprint.update(b"\0")
    fingerprint.update(source_sha.encode("ascii"))
    bundle_id = fingerprint.hexdigest()

    return {
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "bundle_id": bundle_id,
        "task_id": task_id,
        "core_version": core_version,
        "source": {
            "filename": source_path.name,
            "bytes": source_bytes,
            "sha256": source_sha,
            "attribution": source_attribution,
        },
        "clips": bundle_clips,
        "model_scores": validated_scores,
    }


def validate_bundle(bundle) -> dict:
    """Validate the shape of a bundle dict and return it unchanged.

    Raises :class:`BundleError` for any missing/invalid field. This does *not*
    check that referenced clip files exist on disk; it only validates the
    JSON contract. Consumers that receive a bundle without its files should
    call this first, then verify file presence separately.
    """
    if not isinstance(bundle, dict):
        raise BundleError("bundle must be an object")
    if bundle.get("bundle_schema_version") != BUNDLE_SCHEMA_VERSION:
        raise BundleError("unsupported bundle_schema_version")
    for field in ("bundle_id", "task_id", "core_version"):
        value = bundle.get(field)
        if not isinstance(value, str) or not value:
            raise BundleError(f"bundle.{field} must be a non-empty string")
    _validate_task_id(bundle["task_id"])
    if (not isinstance(bundle["bundle_id"], str)
            or len(bundle["bundle_id"]) != 64
            or not all(c in "0123456789abcdef" for c in bundle["bundle_id"])):
        raise BundleError("bundle_id must be a 64-char lowercase hex digest")
    source = bundle.get("source")
    if not isinstance(source, dict):
        raise BundleError("bundle.source must be an object")
    for field in ("filename", "sha256"):
        if not isinstance(source.get(field), str) or not source[field]:
            raise BundleError(f"bundle.source.{field} must be a non-empty string")
    if not isinstance(source.get("bytes"), int) or source["bytes"] < 0:
        raise BundleError("bundle.source.bytes must be a non-negative int")
    if not isinstance(source.get("attribution"), str):
        raise BundleError("bundle.source.attribution must be a string")
    clips = bundle.get("clips")
    if not isinstance(clips, list) or not clips:
        raise BundleError("bundle.clips must be a non-empty list")
    seen: set[str] = set()
    for clip in clips:
        if not isinstance(clip, dict):
            raise BundleError("each clip must be an object")
        clip_id = clip.get("clip_id")
        if not isinstance(clip_id, str) or not clip_id:
            raise BundleError("clip.clip_id must be a non-empty string")
        if clip_id in seen:
            raise BundleError("duplicate clip_id in bundle")
        seen.add(clip_id)
        filename = clip.get("filename")
        if (not isinstance(filename, str) or not filename
                or "/" in filename or "\\" in filename):
            raise BundleError("clip.filename must be a bare relative name")
        if not isinstance(clip.get("bytes"), int) or clip["bytes"] < 0:
            raise BundleError("clip.bytes must be a non-negative int")
        sha = clip.get("sha256")
        if (not isinstance(sha, str) or len(sha) != 64
                or not all(c in "0123456789abcdef" for c in sha)):
            raise BundleError("clip.sha256 must be a 64-char lowercase hex digest")
        if not isinstance(clip.get("duration_seconds"), (int, float)):
            raise BundleError("clip.duration_seconds must be numeric")
        rng = clip.get("range")
        if (not isinstance(rng, dict)
                or not isinstance(rng.get("start"), str)
                or not isinstance(rng.get("end"), str)):
            raise BundleError("clip.range must be {start, end} strings")
        cap = clip.get("captions")
        if (not isinstance(cap, dict)
                or not isinstance(cap.get("available"), bool)
                or (cap.get("filename") is not None
                    and not isinstance(cap.get("filename"), str))):
            raise BundleError("clip.captions must be {available, filename}")
    scores = bundle.get("model_scores")
    if not isinstance(scores, list):
        raise BundleError("bundle.model_scores must be a list")
    for score in scores:
        if not isinstance(score, dict):
            raise BundleError("each model score must be an object")
        for field in ("clip_id", "model_name", "model_version", "produced_at"):
            if not isinstance(score.get(field), str) or not score[field]:
                raise BundleError(f"model score.{field} must be a non-empty string")
        if score["clip_id"] not in seen:
            raise BundleError("model score references unknown clip_id")
        if not isinstance(score.get("score"), (int, float)) or isinstance(score.get("score"), bool):
            raise BundleError("model score.score must be numeric")
        # approval must be explicitly null on the wire; it is not a model output.
        if "approval" not in score or score["approval"] is not None:
            raise BundleError("model score.approval must be null (distinct from approval)")
    return bundle
