# Video Analysis — plan

## Purpose

Build reusable video-analysis and clipping software with reproducible development examples. Limited local compute is a reason to use suitable hosted development runners, not a claim to unrestricted production processing.

## Public core and outputs

1. Media metadata inspection and size/duration validation.
2. Transcript/timestamp schema validation and deterministic segment selection.
3. FFmpeg-based cutting and subtitle rendering with safe process arguments and output paths.
4. Optional transcription/model adapters behind explicit opt-in and cost/download limits.
5. Synthetic or licensed fixtures, tests and small benchmark reports.

An existing private pipeline may be reviewed as a source candidate, but no implementation, data or history has been copied here. License/provenance review is required before extraction.

## Private runtime

Keep real input libraries, private transcripts, unpublished clips, account cookies, credentials, preferences and production queues in a separate private working directory and access-controlled storage. Public CI must not check out a private data repo or accept secret media URLs. See [data boundary](DATA_BOUNDARY.md).

## Development milestones

1. Select a code license; define CLI and schemas; create synthetic fixtures.
2. Implement local-file inspection and clip rendering from supplied timestamps. No scraping, LLM or transcription is required for this first slice.
3. Test malformed files, traversal/symlinks, oversized inputs, invalid timestamps, subprocess timeouts and secret-free logs.
4. Add a bounded manual development workflow only after review. Proposed caps: one fixture <=2 minutes / <=100 MB, one job at a time, 15-minute timeout, no schedule, no private credentials, no automatic artifact upload or data commit.
5. Record runtime, peak disk/memory and output correctness; only then evaluate optional transcription/AI with separate budgets.

These are proposed controls, not implemented protections. Disable network ingestion initially. If later approved, constrain permitted schemes/hosts, redirects, download sizes and source rights; never accept arbitrary URLs from untrusted issues into a privileged job.

## Dataset generation versus production processing

- Good development example: produce known clips from a synthetic source and check boundaries/captions.
- Case-specific proposal: a small reproducible benchmark derived from redistributable footage, with attribution, dataset documentation, published findings and capped compute. Confirm suitability before recurring use.
- Outside this initial scope: continuously process private media queues or download arbitrary platform videos just because standard public runners have no minute charge.

More files do not automatically make a workflow appropriate; purpose, permissions, burden and actual use matter. Seek GitHub Support guidance for sustained workloads. Do not auto-commit large media or accumulate public artifacts as a storage backend.

## Runner and AI reality

As documented September 29, 2026, public Linux x64 `ubuntu-latest` standard runners provide 4 CPU cores, 16 GB RAM and 14 GB SSD. They are ephemeral and have no standard GPU. Actual free disk varies with tools/images. Smaller tasks may fit; throughput for real videos is unmeasured.

Eligible public standard-runner minutes are free, but runtime/concurrency/storage/acceptable-use limits remain. GitHub's separately named larger runners (including GPU offerings) are paid even for public repositories. The proposed 15-minute cap is deliberately below the standard hosted job maximum, not a promise of uninterrupted service.

Ordinary FFmpeg code does not consume AI credits. Local speech models still consume CPU and download/disk budget. External AI calls use the chosen provider's quotas; Copilot subscription credits are not a generic API key or transferable model balance. Cloud and local coding agents can both consume substantial model context; compare measured workloads rather than assuming a fixed cloud token penalty.

## References

- [Runner specifications](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- [Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [Actions limits](https://docs.github.com/en/actions/reference/limits)
- [Actions terms](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features#actions)
- [Cloud-agent costs](https://docs.github.com/en/copilot/concepts/agents/cloud-agent/about-cloud-agent#copilot-cloud-agent-usage-costs)
- [GitHub Support](https://support.github.com/)