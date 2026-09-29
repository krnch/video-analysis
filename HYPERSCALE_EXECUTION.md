# Manager execution brief — Video Analysis

Updated September 29, 2026. **Task specification; not an enabled worker or schedule.** Begin implementation when the owner delegates this brief. This documentation PR does not itself launch cloud agents, run media batches or approve spending.

## Mission

Build the reusable engine that produces useful short clips and structured analysis for a separate private review application. The deliverable is functioning software and verifiable clip output, not another plan-only PR. Tests support the product.

Coordinate with [web-scraper](https://github.com/krnch/web-scraper): start with **two active implementation workers total, at most one per repository**. “Hyperscale” is the owner's coordination label, not an existing command or deployed dispatcher in this repo. Do not assume a larger concurrency quota for cloud agents from an Actions plan table.

## Manager's first actions

1. Read this brief, [PLAN.md](PLAN.md), [DATA_BOUNDARY.md](DATA_BOUNDARY.md) and [EXECUTION.md](EXECUTION.md); inspect current code/PRs so work is not duplicated.
2. Record a concrete task ID, implementation branch, worker and current status in the execution page. If this documentation PR is unmerged, read its branch as the specification; do not silently merge it or unrelated PRs.
3. Start **V1** below using new generic code and synthetic fixtures. Do not copy private source/history or real media. Record the intended license decision; do not infer redistribution rights for third-party code/data.
4. Deliver working code, tests, exact reproduction instructions and an implementation PR. Update evidence and the next task after each completed milestone. Do not replace implementation with more planning-only documents.

## Work packages

### V1 — deterministic usable clipper (first implementation PR)

- Create a small installable CLI/library using an appropriate maintained stack; Python with FFmpeg/ffprobe is a sensible baseline. Document supported versions and explicit missing-tool errors.
- Inspect an explicit local input file: format, duration, streams and byte size.
- Accept a validated JSON list of requested start/end timestamps and an explicit output directory. Render clips with safe subprocess arguments, bounded execution and collision-safe file names.
- Generate a tiny synthetic video during tests; no internet download, transcription model or model API is needed for this milestone.
- Emit clips plus a versioned result manifest suitable for a private importer. Runtime-generated media must not be committed.
- Enforce proposed initial bounds: source <=2 minutes / <=100 MB, <=3 output clips, <=100 MB total output, and a configurable process deadline below the 15-minute proposed demo job cap. Reject invalid or excessive requests before expensive processing where possible.

**Acceptance:** a documented command generates playable clips from the synthetic source; ffprobe checks timestamps/durations within a documented tolerance; manifest hashes/sizes match actual files; invalid timestamps, oversized files, output traversal/collisions and hanging subprocesses fail clearly without partial success.

### V2 — analysis and captions

- Add a supplied-transcript adapter and validated timestamped text schema.
- Add deterministic candidate selection and subtitle rendering without model calls; separate suggested quality scores from human ratings.
- Design optional speech/model adapters with mock tests, explicit opt-in and bounded inputs/output. Do not enable live inference or download large models in the default path.
- Reuse cached analysis by input hash where appropriate; do not rerender identical completed work without a reason.

**Acceptance:** known transcript fixtures produce expected candidates/captions; absent AI credentials do not break the deterministic path; mock adapter errors/timeouts are bounded; model suggestions never override human decisions.

### V3 — interoperable result bundles and measurement

- Implement the result contract below, deterministic task/output identities and offline bundle validation.
- Add a mock/local consumer test for duplicate, incomplete, corrupt and out-of-order delivery. Do not invent or expose a live private application's API.
- Record accepted clip count, source minutes, wall time, output size, failures and retries. Record measured AI usage when available; otherwise use `unknown`, not zero.

**Acceptance:** a separate consumer can validate and import the bundle once without duplicate clips; repeated imports preserve simulated human ratings. No real upload endpoint or private credential is needed.

## Private-result contract

Proposed versioned manifest: `schema_version`, `task_id`, core version/commit, source hash/provenance, and clip entries containing an opaque `clip_id`, safe relative file name, SHA-256, byte length, duration, start/end times and caption availability. Optional algorithm/model scores and provenance are distinct from human approval.

Final product flow: approved input -> temporary processing -> clips/manifest -> private delivery -> private clip library, ratings and review. Git is for code, not private media. Do not put real clips, transcripts, private source URLs or private preferences into commits, PR descriptions, logs, caches, artifacts or releases.

A future real delivery adapter must use authenticated task-scoped access, durable acknowledgements and idempotent import. If the destination machine is offline, use an approved private durable inbox or pause admission; ephemeral runner storage is not persistence. Clean temporary results after durable acknowledgement or explicit bounded failure handling, not before a falsely reported success.

## Coordination and capacity

- One manager owns the global two-worker admission counter. GitHub concurrency groups alone do not coordinate two different repositories. Until that control exists, dispatch manually and inspect active sessions before starting another.
- One implementation writer per repository; independent tests can run only within the approved total job/resource budget. No duplicate assignments, uncontrolled subagent fan-out or automatic retry loop.
- Run the first approved benchmark at concurrency one, then two. A five-standard-job experiment needs a separate evidence review and approval; it is not automatically five cloud-agent sessions.
- Record queued time, execution time, accepted output and credits separately. Stop increasing concurrency when useful throughput stops improving or quality/cost worsens.
- Honor 429/reset instructions with bounded backoff. Stop on exhausted budgets, authorization uncertainty, repeated failures or empty useful backlog. No token/account/IP rotation, filler tasks or keep-alive work.

## Runtime gates — do not block offline implementation on these

Proceed with V1–V3 using local/synthetic fixtures while reporting these gates separately:

- Owner-approved source permissions, actual source files and output destination.
- Supported AI access and an explicit numeric credit/paid-spend cap before autonomous model dispatch or runtime inference. An organization's unused credits are not automatically available to arbitrary scripts.
- Authenticated private delivery and verified private application integration before real output transfer.
- Suitability of the specific workload under GitHub terms before relying on Actions as sustained application compute.
- Reviewed workflow permission/dependency model before adding any executable CI/demo workflow. Start with manual, bounded, secret-free development runs; no schedule, private-repo checkout, public media upload or paid larger/GPU runner by default.

Code drafting/ordinary offline tests in the owner's delegated session are distinct from permission to launch unattended cloud sessions. If a runtime gate is missing, report the exact blocker without pretending the core cannot be implemented.

## Manager completion report

For each milestone update [EXECUTION.md](EXECUTION.md) with: task ID, branch/commit/PR, implemented features, commands actually run, test totals and failures, fixture provenance, output/resource measurements, known limits and next action. Never claim a synthetic test proves live delivery or source access.

Do not merge PRs automatically or publish clips. The owner reviews releases, real-data processing and publishing separately.

## Definition of first useful result

**A reproducible CLI produces valid short clips and a verified private-importable manifest, with tests and no private-data exposure.** Subsequent work improves selection, captions and throughput rather than manufacturing extra workflow activity.