# Video Analysis

A reusable local video clipper, with future plans for transcript adapters, highlight selection and subtitles.

**Status: local-only clipper with a versioned result contract and mock consumer.** There is no model integration, processed dataset or media-processing Actions workflow. This repository does not provide an unlimited free video-processing service.

## Clip a local video

Requires Python 3.9+ and `ffmpeg`/`ffprobe` on `PATH`. Install with `python3 -m pip install .` (preferably in a virtual environment). From a directory **outside this repository**, create a synthetic source and render two clips:

```sh
ffmpeg -v error -f lavfi -i testsrc2=size=160x120:rate=25 -t 3 -c:v mpeg4 /tmp/clipper-source.mp4
video-clipper /tmp/clipper-source.mp4 /tmp/clipper-results \
  --clip 00:00:00.200 00:00:01.200 \
  --clip 00:00:01.500 00:00:02.500
ffprobe -v error -show_entries format=duration -of json /tmp/clipper-results/clip-001.mp4
```

The destination must not already exist; remove it before repeating this example. No download, timestamp inference, transcription or network access is performed. Each `--clip` takes a start and end in `HH:MM:SS[.ffffff]` format; start must precede end and both must be within the probed source duration. The package also exposes `video_clipper.clip(source, output_dir, ranges)` for local Python callers.

`manifest.json` is a schema-version-3 result bundle. It contains a task UUID, core version, and source provenance (`local_file`, basename and SHA-256; never the source path). Each clip has an opaque UUID, a filename relative to the results directory, byte length, SHA-256, rendered duration, requested start/end range, and `captions_available`. Optional `model_scores` entries carry a model name, numeric score and provenance; they are model outputs, not approval or human ratings. Clips are re-encoded as MP4 (MPEG-4 video, AAC audio if present). The rendered duration must be within **0.25 seconds** of the requested end minus start; otherwise the entire operation fails. Seeking and frame boundaries can cause smaller differences.

`result_consumer.MockConsumer` is a local-only SQLite mock for testing private-consumer compatibility. It validates the whole manifest and every clip hash before importing, accepts clips in any manifest order, rejects duplicate IDs and corrupt or partial bundles, and treats an identical task re-import as a no-op. Human ratings are stored separately and are not overwritten by re-import. It has no network receiver, credentials, or upload API; use only synthetic/local fixtures.

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