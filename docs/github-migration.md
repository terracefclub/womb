# GitHub primary migration

GitHub is the intended primary for this public repository at
https://github.com/terracefclub/womb. GitLab history, issues, merge-request
conversations and historical pipelines remain at https://gitlab.com/terrace/tech/womb.
The migration preserves Git commits; it does not recreate historical discussions or
claim their approvals were GitHub approvals.

The five Actions checks execute the exact existing `.gitlab-ci.yml` commands and
immutable container images through `tools/github-ci.py`. The adapter rejects
unsupported YAML instead of silently omitting checks. Validate it without Docker:

```sh
direnv exec . python tools/github-ci.py check-supervision --validate
direnv exec . python tests/platform/test_github_ci.py
```

The jobs have read-only repository permissions, ten-minute deadlines, no deployment
steps, and cancel obsolete runs. Native Nix/VM acceptance remains a separate,
supervised Linux-builder operation; it is not an always-on Actions runner.

Migration of reviewed changes and one-way GitLab mirroring are still in progress.
