# Video Analysis

A planned reusable toolkit for video inspection, transcript adapters, highlight selection, subtitles and clip rendering.

**Status: documentation-only.** There is no runnable CLI, model integration, processed dataset or Actions workflow yet. This repository does not provide an unlimited free video-processing service.

## Intended first version

- Inspect a local video and validate duration, format and file size.
- Accept supplied timestamps/transcripts; select and render clips deterministically.
- Add optional transcription and model-based selection later, with explicit compute and cost limits.
- Test on synthetic or permissively licensed samples with attribution and provenance.

Real media libraries, private transcripts, credentials and processing queues belong in a separate private execution environment. Publishing the core does not publish or migrate any existing installation.

## Documentation

- [Plan and compute constraints](PLAN.md)
- [Execution state and acceptance checklist](EXECUTION.md)
- [Data and publication boundary](DATA_BOUNDARY.md)

A code license must be selected before releasing implementation code. No rights to third-party videos or datasets are implied by this repository being public.