# Disposable rehearsal resource declarations

This project is an input to the reviewed lease runner. It declares resources
only for the exact `foundation`, `original` or `replacement` stack selected
by that runner. The runner supplies `TERRACE_REHEARSAL_INPUT` as the absolute
path to an owner-only 0600 JSON file and stages this directory beside the
parent's locked dependencies, `budget.ts` and `tsconfig.json`.

The foundation declares the run SSH key and an SSH-only firewall. Each
cohort declares two servers with two explicitly attached Primary IPv4
addresses. The sole output, `owned`, has numeric ID arrays for `servers`,
`primary_ips`, `firewalls` and `ssh_keys`. Cohort arrays are ordered app
then relay. The lease runner verifies those IDs against provider inventory.

Validate from the repository root with the parent project's locked packages:

```sh
direnv exec . sh -c 'cd infra/cloud && bun install --frozen-lockfile --ignore-scripts'
direnv exec . sh -c 'cd infra/cloud && bun test rehearsal-create/rehearsal.test.ts'
direnv exec . sh -c 'cd infra/cloud && bun x --no-install tsc -p rehearsal-create/tsconfig.json'
```

The tests use Pulumi SDK mocks to inspect the declared resource graph.
