# Video Analysis

A reusable local video clipper with validated transcript adapters, deterministic highlight candidates and burned-in captions.

**Status: V1 local-only clipper.** There is no model integration, processed dataset or media-processing Actions workflow. This repository does not provide an unlimited free video-processing service.

## Clip a local video

Requires Python 3.9+ and `ffmpeg`/`ffprobe` on `PATH`. Install with `python3 -m pip install .` (preferably in a virtual environment). From a directory **outside this repository**, create a synthetic source and render two clips:

```sh
ffmpeg -v error -f lavfi -i testsrc2=size=160x120:rate=25 -t 3 -c:v mpeg4 /tmp/clipper-source.mp4
video-clipper /tmp/clipper-source.mp4 /tmp/clipper-results \
  --clip 00:00:00.200 00:00:01.200 \
  --clip 00:00:01.500 00:00:02.500
ffprobe -v error -show_entries format=duration -of json /tmp/clipper-results/clip-001.mp4
```

The destination must not already exist; remove it before repeating this example. No download or network access is performed. Each `--clip` takes a start and end in `HH:MM:SS[.ffffff]` format; start must precede end and both must be within the probed source duration. The package also exposes `video_clipper.clip(source, output_dir, ranges, captions=None)` for local Python callers. Captions are supplied as validated `{start, end, text}` records and are burned into the video.

`manifest.json` has `schema_version: 1` without captions, or `schema_version: 2` with a `captions` entry. Each clip entry contains `id`, `filename`, `bytes`, `sha256`, and `duration_seconds`. Transcript JSON uses a `segments` array with ordered, non-overlapping `start`, `end`, and non-empty `text` fields. `transcript.load_transcript`, `highlight_candidates`, and `analyze_transcript` provide validation, deterministic suggested scores, and content-hash cache reuse. Suggested scores and `human_rating` are separate fields; no model or credentials are needed.

Limits: one regular local source at most 120 seconds and 100,000,000 bytes, 1–3 clips, at most 100,000,000 bytes of clip output, and a shared subprocess timeout of 840 seconds (14 minutes). Paths containing `..` or symlink components are rejected. Errors return a nonzero exit code and leave no destination or partial result; already-existing destinations are never intentionally replaced. Output names are fixed, collision-free within each run, and contain no user-supplied text. Keep all media and result manifests outside git; `.gitignore` adds accidental-staging guards, not a confidentiality boundary.

Run the synthetic integration tests with `python3 -m unittest discover -s tests -v`.

## Future scope

- Extend inspection with format-specific metadata validation.
- Accept supplied transcripts and select highlights automatically.
- Add optional transcription and model-based selection later, with explicit compute and cost limits.
- Test on synthetic or permissively licensed samples with attribution and provenance.

Real media libraries, private transcripts, credentials and processing queues belong in a separate private execution environment. Publishing the core does not publish or migrate any existing installation.

## Documentation

- [Plan and compute constraints](PLAN.md)
- [Execution state and acceptance checklist](EXECUTION.md)
- [Data and publication boundary](DATA_BOUNDARY.md)

A code license must be selected before releasing implementation code. No rights to third-party videos or datasets are implied by this repository being public.