"""Local-only mock consumer for validating and importing result bundles."""

import hashlib
import json
import math
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path


class BundleError(ValueError):
    """Invalid or conflicting result bundle."""


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UUID_HEX = re.compile(r"^[0-9a-f]{32}$")
_MAX_BUNDLE_BYTES = 100_000_000
_MAX_MANIFEST_BYTES = 1_000_000


def _is_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_model_scores(scores):
    if not isinstance(scores, list):
        raise BundleError("model_scores must be a list")
    for score in scores:
        if (
            not isinstance(score, dict)
            or not isinstance(score.get("model"), str)
            or not score["model"]
            or not _is_number(score.get("score"))
            or not isinstance(score.get("provenance"), str)
            or not score["provenance"]
        ):
            raise BundleError("each model score requires model, score, and provenance")


def _validate_bundle(bundle_dir):
    root = Path(bundle_dir).expanduser().absolute()
    if any(part.is_symlink() for part in (root, *root.parents)):
        raise BundleError("symlink paths are not allowed")
    if not root.is_dir():
        raise BundleError("bundle must be an existing local directory")

    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise BundleError("bundle manifest is missing or invalid")
    if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise BundleError("bundle manifest is too large")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BundleError("bundle manifest is invalid JSON") from exc

    if not isinstance(manifest, dict) or manifest.get("schema_version") != 3:
        raise BundleError("unsupported result bundle schema")
    task_id = manifest.get("task_id")
    try:
        uuid.UUID(task_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise BundleError("task_id must be a UUID") from exc
    if not isinstance(task_id, str) or str(uuid.UUID(task_id)) != task_id:
        raise BundleError("task_id must be a canonical UUID")
    if not isinstance(manifest.get("core_version"), str) or not manifest["core_version"]:
        raise BundleError("core_version is required")
    source = manifest.get("source")
    if (
        not isinstance(source, dict)
        or source.get("provenance") != "local_file"
        or not isinstance(source.get("filename"), str)
        or not source["filename"]
        or Path(source["filename"]).name != source["filename"]
        or "\\" in source["filename"]
        or any(ord(char) < 32 for char in source["filename"])
        or not isinstance(source.get("sha256"), str)
        or not _SHA256.fullmatch(source["sha256"])
    ):
        raise BundleError("source provenance is invalid")

    clips = manifest.get("clips")
    if not isinstance(clips, list) or not 1 <= len(clips) <= 3:
        raise BundleError("bundle must contain between one and three clips")
    clip_ids = set()
    filenames = set()
    total_bytes = 0
    for clip in clips:
        if not isinstance(clip, dict):
            raise BundleError("clip entry is invalid")
        clip_id = clip.get("id")
        if not isinstance(clip_id, str) or not _UUID_HEX.fullmatch(clip_id):
            raise BundleError("clip id must be an opaque UUID")
        if clip_id in clip_ids:
            raise BundleError("duplicate clip id")
        clip_ids.add(clip_id)

        filename = clip.get("filename")
        if (
            not isinstance(filename, str)
            or not filename
            or Path(filename).name != filename
            or "\\" in filename
            or any(ord(char) < 32 for char in filename)
            or filename in {".", "..", "manifest.json"}
            or filename in filenames
        ):
            raise BundleError("clip filename must be unique and relative")
        filenames.add(filename)
        path = root / filename
        if path.is_symlink() or not path.is_file():
            raise BundleError("bundle is missing a clip file")
        byte_length = clip.get("bytes")
        if type(byte_length) is not int or byte_length <= 0:
            raise BundleError("clip byte length is invalid")
        total_bytes += byte_length
        if total_bytes > _MAX_BUNDLE_BYTES:
            raise BundleError("bundle exceeds 100 MB")
        digest = clip.get("sha256")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise BundleError("clip SHA-256 is invalid")
        if path.stat().st_size != byte_length or _hash_file(path) != digest:
            raise BundleError("clip integrity check failed")

        duration = clip.get("duration_seconds")
        time_range = clip.get("range_seconds")
        if (
            not _is_number(duration)
            or duration <= 0
            or not isinstance(time_range, dict)
            or not _is_number(time_range.get("start"))
            or not _is_number(time_range.get("end"))
            or time_range["start"] < 0
            or time_range["end"] <= time_range["start"]
            or abs(duration - (time_range["end"] - time_range["start"])) > 0.25
        ):
            raise BundleError("clip duration or range is invalid")
        if type(clip.get("captions_available")) is not bool:
            raise BundleError("captions_available must be a boolean")
        if "model_scores" in clip:
            _validate_model_scores(clip["model_scores"])

    return manifest


class MockConsumer:
    """A local SQLite-backed stand-in; it has no network or upload interface."""

    def __init__(self, database_path):
        self.database_path = Path(database_path)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    manifest_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS clips (
                    clip_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    filename TEXT NOT NULL,
                    clip_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ratings (
                    clip_id TEXT PRIMARY KEY REFERENCES clips(clip_id),
                    rating_json TEXT NOT NULL
                );
                """
            )

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def import_bundle(self, bundle_dir):
        manifest = _validate_bundle(bundle_dir)
        task_id = manifest["task_id"]
        manifest_json = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT manifest_json FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if existing:
                if existing[0] != manifest_json:
                    raise BundleError("task_id already exists with different bundle content")
                return {"task_id": task_id, "clips_imported": 0}
            try:
                connection.execute(
                    "INSERT INTO tasks (task_id, manifest_json) VALUES (?, ?)",
                    (task_id, manifest_json),
                )
                for clip in manifest["clips"]:
                    connection.execute(
                        "INSERT INTO clips (clip_id, task_id, filename, clip_json) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            clip["id"],
                            task_id,
                            clip["filename"],
                            json.dumps(clip, sort_keys=True, separators=(",", ":")),
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise BundleError("bundle conflicts with previously imported content") from exc
        return {"task_id": task_id, "clips_imported": len(manifest["clips"])}

    def set_rating(self, clip_id, rating):
        rating_json = json.dumps(rating, sort_keys=True, separators=(",", ":"))
        with self._connection() as connection:
            if not connection.execute(
                "SELECT 1 FROM clips WHERE clip_id = ?", (clip_id,)
            ).fetchone():
                raise BundleError("cannot rate an unknown clip")
            connection.execute(
                "INSERT INTO ratings (clip_id, rating_json) VALUES (?, ?) "
                "ON CONFLICT(clip_id) DO UPDATE SET rating_json = excluded.rating_json",
                (clip_id, rating_json),
            )

    def get_rating(self, clip_id):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT rating_json FROM ratings WHERE clip_id = ?", (clip_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None
