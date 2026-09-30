"""Validated transcript input and deterministic highlight candidates."""

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import hashlib
import json
from pathlib import Path
import re
from typing import Optional


class TranscriptError(ValueError):
    """Invalid transcript input."""


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str
    speaker: Optional[str] = None
    confidence: Optional[float] = None


class TranscriptAdapter:
    """Adapter for the supplied JSON transcript format."""

    def load(self, path):
        return load_transcript(path)


def _number(value, name):
    if isinstance(value, bool):
        raise TranscriptError(f"{name} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise TranscriptError(f"{name} must be a number") from None
    if result < 0 or result != result or result in (float("inf"), float("-inf")):
        raise TranscriptError(f"{name} must be a finite non-negative number")
    return result


def validate_transcript(value):
    """Validate a JSON-compatible transcript and return its segments."""
    if isinstance(value, list):
        records = value
    elif isinstance(value, dict) and isinstance(value.get("segments"), list):
        records = value["segments"]
    else:
        raise TranscriptError("transcript must contain a segments array")
    segments = []
    previous_end = 0.0
    for record in records:
        if not isinstance(record, dict):
            raise TranscriptError("each transcript segment must be an object")
        start = _number(record.get("start"), "start")
        end = _number(record.get("end"), "end")
        text = record.get("text")
        if not isinstance(text, str) or not text.strip():
            raise TranscriptError("segment text must be a non-empty string")
        if end <= start or start < previous_end:
            raise TranscriptError("segments must be ordered and have positive duration")
        confidence = record.get("confidence")
        if confidence is not None:
            confidence = _number(confidence, "confidence")
            if confidence > 1:
                raise TranscriptError("confidence must be between 0 and 1")
        speaker = record.get("speaker")
        if speaker is not None and not isinstance(speaker, str):
            raise TranscriptError("speaker must be a string")
        segments.append(TranscriptSegment(start, end, text.strip(), speaker, confidence))
        previous_end = end
    if not segments:
        raise TranscriptError("transcript must contain at least one segment")
    return tuple(segments)


def load_transcript(path):
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TranscriptError(f"cannot read transcript: {exc}") from exc
    return validate_transcript(value)


_HIGHLIGHT_WORDS = {
    "amazing": 3, "important": 2, "secret": 2, "simple": 2, "best": 2,
    "never": 2, "because": 1, "how": 1, "why": 1,
}


def highlight_candidates(segments, max_candidates=3):
    """Return reproducible candidates with suggested scores, not ratings."""
    if max_candidates < 1:
        raise TranscriptError("max_candidates must be positive")
    if isinstance(segments, dict):
        segments = segments.get("segments", [])
    segments = validate_transcript({"segments": [
        {"start": s.start, "end": s.end, "text": s.text,
         "speaker": s.speaker, "confidence": s.confidence}
        if isinstance(s, TranscriptSegment) else s for s in segments
    ]})
    scored = []
    for index, segment in enumerate(segments):
        words = re.findall(r"[a-z0-9']+", segment.text.lower())
        keyword_score = sum(_HIGHLIGHT_WORDS.get(word, 0) for word in words)
        punctuation_score = segment.text.count("!") + segment.text.count("?")
        length_score = min(len(words), 20) / 20
        suggested_score = round(keyword_score + punctuation_score + length_score, 6)
        scored.append((suggested_score, index, segment))
    scored.sort(key=lambda item: (-item[0], item[1]))
    result = []
    for score, index, segment in scored[:max_candidates]:
        result.append({
            "id": f"candidate-{index + 1:03d}",
            "start": segment.start,
            "end": segment.end,
            "text": segment.text,
            "suggested_score": score,
            "human_rating": None,
        })
    return result


def analysis_cache_key(path):
    """Return the SHA-256 key used to reuse a transcript analysis."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def analyze_transcript(path, cache_dir=None, max_candidates=3):
    """Load and analyze once, reusing a content-addressed cache when supplied."""
    key = analysis_cache_key(path)
    cache = Path(cache_dir) if cache_dir is not None else None
    cached = cache / f"{key}.json" if cache else None
    if cached and cached.is_file():
        return json.loads(cached.read_text(encoding="utf-8"))
    segments = load_transcript(path)
    result = {"schema_version": 1, "input_sha256": key,
              "candidates": highlight_candidates(segments, max_candidates)}
    if cached:
        cache.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def optional_analysis(adapter, source, *, enabled=False, budget=1, timeout=None):
    """Run an explicitly enabled, bounded optional adapter."""
    if not enabled:
        raise TranscriptError("optional analysis requires explicit opt-in")
    if budget < 1:
        raise TranscriptError("optional analysis budget must be positive")
    if adapter is None or not callable(adapter):
        raise TranscriptError("optional analysis adapter is required")
    timeout = budget if timeout is None else timeout
    if timeout <= 0:
        raise TranscriptError("optional analysis timeout must be positive")
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(adapter, source, budget=budget)
    try:
        result = future.result(timeout=timeout)
    except TimeoutError as exc:
        future.cancel()
        raise TranscriptError("optional analysis adapter timed out") from exc
    except Exception as exc:
        raise TranscriptError("optional analysis adapter failed") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return result


detect_highlights = highlight_candidates
