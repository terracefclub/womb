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

GitHub checks were verified before disabling duplicate GitLab pipelines. The
GitLab job definitions stay in place as the canonical container commands and for
historical audit; the exact `workflow: rules: when: never` header prevents new
GitLab pipelines. Mirror pushes additionally use `ci.skip`.

## One-way mirror

`.github/workflows/gitlab-mirror.yml` runs only on published pushes or manual
dispatch, never on pull requests. It checks out trusted `trunk` code and uses a
distinct write-enabled GitLab project deploy key stored as the GitHub Actions
secret `GITLAB_MIRROR_KEY`. The key has no access to other projects. Protected
GitLab `trunk` must explicitly allow that deploy key; existing human permissions
and force-push restrictions are retained. The key is generated in protected local
project state; its private bytes are never committed or placed in command args.

The mirror copies only `trunk` and published tags. It makes atomic, non-forced
pushes and reads back every target SHA. It never deletes GitLab refs, preserving
historical review branches. Divergence fails closed and requires investigation.
SSH is pinned to the GitLab.com Ed25519 host key from the
[official settings](https://docs.gitlab.com/user/gitlab_com/#ssh-host-keys-fingerprints),
with strict host checking; temporary identities are mode0600 and removed on exit.
No personal token or GitHub token is sent to GitLab.

Rerun with **Actions → GitLab mirror → Run workflow** on `trunk`. Rotate by
creating a new project deploy key, granting only that key protected-branch write,
replacing the repository Actions secret, verifying a successful mirror, then
revoking the old project key. Keep GitLab issues and MR conversations available;
new development and reviews belong on GitHub. GitHub does not copy historical
GitLab approval or pipeline records.

Migration of the four reviewed changes is still in progress.
