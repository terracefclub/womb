# GitHub primary migration

GitHub is primary for this public repository at
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

Observed hosted mirror verification on 2026-11-06:
[run42194891186](https://github.com/terracefclub/womb/actions/runs/42194891186)
passed and both remote `trunk` refs matched
`e4634389d64fd2e3f8cc44a630a980f43c22fbdc`. The hosted secret lacked a
terminal LF; the key-file writer now supplies only that missing LF, preserving
every other byte. This fixed the measured OpenSSH parse failure while retaining
the same key and permissions. GitHub `trunk` requires the five checks against the
current base and a pull request, including for administrators; force pushes and
deletions are disabled. Independent source review is also required by this
repository's contributor rules.

## Reviewed changes migrated on 2026-11-06

| GitLab audit | GitHub PR | Merged commit |
| --- | --- | --- |
| [!42](https://gitlab.com/terrace/tech/womb/-/merge_requests/42) | [#2](https://github.com/terracefclub/womb/pull/2) | `d59cb678d31168c3eae27c55fbf2ae3ccadce9e0` |
| [!43](https://gitlab.com/terrace/tech/womb/-/merge_requests/43) | [#3](https://github.com/terracefclub/womb/pull/3) | `406322d19c664a5e979ac358943c00d3226cd632` |
| [!40](https://gitlab.com/terrace/tech/womb/-/merge_requests/40) | [#4](https://github.com/terracefclub/womb/pull/4) | `042aef5954b4e6003d745864d59ff38843cfc290` |
| [!41](https://gitlab.com/terrace/tech/womb/-/merge_requests/41) | [#5](https://github.com/terracefclub/womb/pull/5) | `c1e9b9cd5d24648ababb1d6d8759514d23b5ae94` |

The five live GitLab branches were backed up and seeded unchanged before new CI
commits. The original reviewed heads remain in each PR's ancestry and on GitLab.
Normal merges added the CI files; no history was forced. Each PR passed all five
GitHub checks and independent source review before its SHA-guarded squash merge.
The final runtime/workflow data matches reviewed tree
`477d46cde7d685927611ac609f8dbdd4a5152f2a`; additional files implement CI and
this migration. The native all-service acceptance remains the source-matched
1733.06-second run documented in [full-rehearsal.md](full-rehearsal.md), including
originals-off restore, default preservation, explicit scoped purge and all 38
recorded process identities absent. Hosting migration does not claim provider
installation or a real-data cutover.

Clone current development from GitHub:

```sh
git clone git@github.com:terracefclub/womb.git
cd womb
direnv allow .
direnv exec . check-shell
```

Keep GitLab as an optional `gitlab` remote. Never use `git push --mirror` from an
agent worktree's shared bare repository: it contains unpublished local branches.
Historical GitLab source branches, discussions, approvals and pipeline records are
retained instead of being represented as GitHub approvals.
