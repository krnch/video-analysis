"""In-process mock consumer for V3 result bundles.

This module exists to exercise the result-bundle contract end-to-end in
tests. It is **not** a receiver: there is no HTTP endpoint, no socket, and
no unauthenticated ingest path. Callers hand a bundle dict directly to
:meth:`MockConsumer.import_bundle`; that is the only entry point.

The consumer keeps state in memory:

* ``bundles`` — accepted bundles keyed by ``bundle_id``
* ``ratings`` — human ratings keyed by ``(bundle_id, clip_id)``

Behaviour required by the V3 acceptance criteria:

* Duplicate bundles (same ``bundle_id``) are rejected with a distinct
  status so a repeated import is a no-op for the bundle itself.
* Corrupt bundles (schema-invalid or hash-mismatched clip files, when a
  clip directory is supplied) are rejected without mutating state.
* Partial bundles (missing referenced clip files, when a clip directory
  is supplied) are rejected without mutating state.
* Bundles arriving out of order (e.g. a later ``task_id`` before an
  earlier one) are accepted independently; ordering is not required.
* Human ratings survive repeated imports of the same bundle: an idempotent
  re-import does not clear ratings previously attached to that bundle.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from result_bundle import BundleError, validate_bundle


class ConsumerError(ValueError):
    """Raised when a bundle cannot be imported."""


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class MockConsumer:
    """Local, in-memory sink for result bundles.

    No network I/O is performed. Instances are not thread-safe; tests are
    single-threaded.
    """

    def __init__(self) -> None:
        self.bundles: dict[str, dict] = {}
        # (bundle_id, clip_id) -> {"rating": int, "note": str}
        self.ratings: dict[tuple[str, str], dict] = {}

    # -- import ----------------------------------------------------------

    def import_bundle(self, bundle, clip_dir=None) -> str:
        """Import ``bundle``; return one of ``"accepted"`` or ``"duplicate"``.

        ``clip_dir``, when supplied, is the local directory that contains
        the clip files referenced by the bundle. If given, each clip file
        must exist and its SHA-256 and byte length must match the bundle
        entry (partial/corrupt detection). If omitted, only the JSON
        contract is validated.

        Raises :class:`ConsumerError` for corrupt or partial bundles.
        Never partially applies a bundle: on any error the consumer state
        is unchanged.
        """
        try:
            validate_bundle(bundle)
        except BundleError as exc:
            raise ConsumerError(f"corrupt bundle: {exc}") from exc

        bundle_id = bundle["bundle_id"]
        existing = self.bundles.get(bundle_id)
        if existing is not None:
            # Idempotency: a repeat import of the same bundle_id is a no-op.
            # If the payload disagrees with what we already accepted, that is
            # a distinct collision and must be surfaced (does not touch state).
            if existing != bundle:
                raise ConsumerError(
                    "bundle_id collision with different payload")
            return "duplicate"

        if clip_dir is not None:
            clip_dir = Path(clip_dir)
            if not clip_dir.is_dir():
                raise ConsumerError("clip_dir must be an existing directory")
            for clip in bundle["clips"]:
                path = clip_dir / clip["filename"]
                if not path.is_file():
                    raise ConsumerError(
                        f"partial bundle: missing clip {clip['filename']}")
                actual_sha, actual_bytes = _sha256_file(path)
                if actual_sha != clip["sha256"] or actual_bytes != clip["bytes"]:
                    raise ConsumerError(
                        f"corrupt bundle: clip {clip['clip_id']} hash/size mismatch")

        # Commit only after every check has passed.
        self.bundles[bundle_id] = bundle
        return "accepted"

    # -- human ratings ---------------------------------------------------

    def set_rating(self, bundle_id: str, clip_id: str,
                   rating: int, note: str = "") -> None:
        """Attach or replace a human rating for a clip in an accepted bundle."""
        if bundle_id not in self.bundles:
            raise ConsumerError("unknown bundle_id")
        if not any(c["clip_id"] == clip_id for c in self.bundles[bundle_id]["clips"]):
            raise ConsumerError("unknown clip_id for bundle")
        if not isinstance(rating, int) or isinstance(rating, bool) or not 0 <= rating <= 5:
            raise ConsumerError("rating must be an int in [0, 5]")
        if not isinstance(note, str):
            raise ConsumerError("note must be a string")
        self.ratings[(bundle_id, clip_id)] = {"rating": rating, "note": note}

    def get_rating(self, bundle_id: str, clip_id: str) -> Optional[dict]:
        """Return the rating dict for a clip, or ``None`` if none is set."""
        return self.ratings.get((bundle_id, clip_id))
