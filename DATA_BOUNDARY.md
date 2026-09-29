# Data and publication boundary

## Allowed public material

Generic original/licensed code, schemas, documentation, synthetic fixtures, properly attributed redistributable samples and reviewed non-sensitive benchmark results.

## Keep outside public execution

Personal media libraries, private transcripts, unreleased clips, signed/private URLs, account cookies, credentials, production queues and private configuration. Use separate private working directories and access-controlled storage, not another branch of this public repository.

## Important limitations

- `.gitignore` only reduces accidental staging. It does not prevent forced adds, remove tracked history or stop programs/agents publishing data elsewhere.
- Every branch of a public repository is public. There is no private data branch.
- GitHub Secrets are not a general confidentiality boundary: scripts can leak resulting data through logs, job summaries, artifacts, issues, PRs or AI outputs. Masking is not guaranteed to catch arbitrary transformed values.
- Public repository artifacts are broadly accessible to signed-in readers; they are not private media storage.
- No private-repository checkout or personal token injection in public CI. Do not run untrusted issue/PR commands with credentials.
- A video being viewable online is not permission to download, clip or redistribute it. Check source terms, attribution, privacy and derivative-work rights.

Before publishing any output: verify provenance and distribution rights, inspect the output and filenames/metadata, review the exact upload manifest and size/retention policy, and obtain explicit approval. Initial implementation should use synthetic fixtures and no upload step.