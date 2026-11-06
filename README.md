# Womb

Terrace's infrastructure and membership automation.

[GitHub](https://github.com/terracefclub/womb) is primary;
[GitLab](https://gitlab.com/terrace/tech/womb) retains historical reviews and issues
and receives the published-ref mirror. See [migration and mirroring](docs/github-migration.md).

The platform is being rebuilt around a small, reproducible NixOS deployment,
with a temporary Hetzner host and a later move to Terrace's own server. Existing
infrastructure and Windmill workflows are preserved while replacements are
verified. A source change is not evidence that a replacement is deployed.

## Develop

With Nix, devenv, and direnv available:

```sh
direnv allow .
direnv exec . true
direnv exec . check-shell
```

The shell installs tools and Git hooks. It needs no provider credentials and
does not log in to cloud accounts or select a production stack. CLI state lives
under `.devenv/state/`; supply credentials through a secret manager or an ignored
local environment, never command arguments or committed files.

GitHub SSH access and API access are separate. Authenticate `gh` for current
pull requests and issues. Use `glab` only for historical GitLab audit records;
its configuration remains local to this worktree.

## Workstreams

- [Architecture, inventory, and decisions](docs/revival.md)
- [Incremental delivery and acceptance criteria](docs/backlog.md)
- [Migration and recovery requirements](docs/migration.md)
- [Access and physical-hardware prerequisites](docs/manual-steps.md)
- [Contributor rules](AGENTS.md)

The root `flake.nix`, `nixos/`, `__main__.py`, and `windmill/` are legacy source.
Do not apply the old Pulumi program as part of the new platform bootstrap.
