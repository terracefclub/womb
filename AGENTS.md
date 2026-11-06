# Working on Womb

Read [docs/revival.md](docs/revival.md) and [docs/backlog.md](docs/backlog.md).
This public repository contains Terrace infrastructure and membership workflows.

- Use your own worktree from `origin/trunk`. Fetch and rebase immediately before
  committing. Do not change another worktree or rewrite existing history.
- Run tools and Git through `direnv exec <worktree>`. Enter the shell once before
  committing so its hooks are installed. Never skip hooks or run the hook runner
  directly. Fix failures at their source.
- Keep credentials, member records, exports, provider state, and database backups
  out of Git and tool output. Error output can contain credentials too. Suppress
  it or use a redactor that fails closed. Do not decrypt files to inspect values.
- Shell entry must work without cloud credentials and must never log in, select
  a production stack, apply infrastructure, or deploy. Keep CLI state local.
- Build NixOS, VM tests, containers, and Rust on the configured Linux build host,
  never a developer's Mac. Use `BUILD_HOST` and a task-specific remote checkout;
  do not hardcode someone's personal infrastructure into Terrace configuration.
- Small PRs, conventional `type(scope): what` titles, 1–3 line descriptions,
  no attribution trailers. Check the complete file list and validation before
  squash-merging. Verify the resulting diff and deployed health afterward.
- Change declarative configuration, not live resources. Pulumi changes require
  an exact targeted preview before a matching targeted apply. Never replace an
  existing server or delete data without a verified backup and explicit scope.
- Preserve Windmill/Documenso/Google Sheets/payment-email contracts. Use synthetic
  records and disabled email delivery in tests. Never send real payment links
  or member messages as a test.
- Every persistent dataset needs an owner, retention policy, off-site backup or
  documented exclusion, and a restore check. A successful backup job is not a
  successful restore. Application removal retains its data by default.
- Use individual accounts and native OIDC where supported; no shared admin
  sessions. Bootstrap must be idempotent. Recovery access must not depend on SSO.
- Use Bun/TypeScript for web work, Rust for new bespoke services, and packages
  from devenv. Prefer simple, bounded components over extra operators/services.
- Report verified evidence separately from assumptions, including dates. Record
  external access or hardware steps in `docs/manual-steps.md`.

GitHub is primary: `https://github.com/terracefclub/womb`, default `trunk`.
Use GitHub pull requests and green Actions checks, then independently review the
complete file list and squash-merge with a source-SHA guard. GitLab retains the
historical issues/MRs and receives a one-way published-ref mirror; do not develop
or merge new changes there. See [the migration runbook](docs/github-migration.md).

The legacy root flake and Pulumi program remain preserved until the replacement
passes validation and cutover is planned. Do not run the legacy shell as a
workaround: it performs interactive cloud authentication and selects production.
