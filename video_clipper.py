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

from transcript import TranscriptSegment, TranscriptError, validate_transcript

MAX_BYTES = 100_000_000
MAX_DURATION = Decimal("120")
MAX_CLIPS = 3
TIME_LIMIT = 840  # Shared budget for all probes and renders, below 15 minutes.
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


def _srt_timestamp(seconds):
    milliseconds = int(round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def _caption_file(temporary, captions, clip_start, clip_end):
    selected = []
    for index, caption in enumerate(captions, 1):
        if isinstance(caption, TranscriptSegment):
            start, end, text = caption.start, caption.end, caption.text
        else:
            start, end, text = caption["start"], caption["end"], caption["text"]
        if end <= clip_start or start >= clip_end:
            continue
        selected.append((max(start, clip_start) - clip_start,
                         min(end, clip_end) - clip_start, text))
    if not selected:
        return None
    path = temporary / "captions.srt"
    path.write_text("\n".join(
        f"{index}\n{_srt_timestamp(start)} --> {_srt_timestamp(end)}\n{text}\n"
        for index, (start, end, text) in enumerate(selected, 1)
    ) + "\n", encoding="utf-8")
    return path


def clip(source, output_dir, ranges, captions=None):
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
    if captions is not None:
        try:
            captions = validate_transcript({"segments": [
                {"start": item.start, "end": item.end, "text": item.text}
                if isinstance(item, TranscriptSegment) else item for item in captions
            ]})
        except (TypeError, KeyError, TranscriptError) as exc:
            raise ClipError(f"invalid captions: {exc}") from exc

    temporary = Path(tempfile.mkdtemp(prefix=".video-clipper-", dir=output_dir.parent))
    try:
        clips = []
        total = 0
        for index, (start, end) in enumerate(parsed, 1):
            name = f"clip-{index:03d}.mp4"
            rendered = temporary / name
            caption_path = _caption_file(temporary, captions, float(start), float(end)) if captions else None
            filters = [f"subtitles={caption_path}"] if caption_path else []
            ffmpeg_args = [
                "ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(source),
                "-ss", str(start), "-t", str(end - start),
                "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "-1",
                "-map_chapters", "-1", "-c:v", "mpeg4", "-q:v", "4",
                "-c:a", "aac", "-pix_fmt", "yuv420p", "-threads", "1",
            ]
            if filters:
                ffmpeg_args.extend(["-vf", ",".join(filters)])
            ffmpeg_args.extend(["-fs", str(MAX_BYTES - total + 1), str(rendered)])
            _run(
                ffmpeg_args,
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
        manifest = {"schema_version": 2 if captions else 1, "clips": clips}
        if captions:
            manifest["captions"] = [
                {"filename": "captions.srt", "format": "srt",
                 "segments": len(captions)}
            ]
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
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
