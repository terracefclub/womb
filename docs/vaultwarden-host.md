# Vaultwarden native artifact preparation

This increment prepares a narrowly pinned native PostgreSQL binary and the
matching official web vault. No runtime module or actual profile is enabled.
The source pin follows the official Nixpkgs1.37.5 recipe at
[`224e5a3d`](https://github.com/NixOS/nixpkgs/blob/224e5a3d70c79442a94d16352f55fb28f0025744/pkgs/by-name/va/vaultwarden/package.nix),
with explicit source/vendor hashes and official web archive hash. No dependency
or image build runs on a developer's Mac.

Run the Linux artifact check under the shared builder lock:

```sh
BUILD_LOCK=/path/to/shared-heavy.lock platform/vaultwarden-package-check
```

The first Rust build has a40-minute total/build bound; use the documented
`run-check` bounds to override it explicitly. The check runs only `--version`
inside the build sandbox with an empty environment, disabled dotenv and an
absent saved-config path. It does not start a service, create accounts, connect
to PostgreSQL or deliver mail. The output must identify the pinned server and
web-vault versions.

The November5 Linux proof at source `66f5df8` passed with `Vaultwarden 1.37.5`
and `Web-Vault 2026.10.1`; the native build also passed all38 upstream tests.
The version-only output is
`/nix/store/zgsmaw0l7g66gyiqcz87168cm8m53nml-terrace-vaultwarden-release-proof-1.37.5`.
All86 frozen platform files matched their recorded hashes. The three outer
job identities and two observed compiler identities were absent after completion;
the isolated build PID namespace was empty on the final check. No application
service, database or account was started by this proof.

The pure initial client helper is
`platform/vaultwarden/policy.ts:vaultwardenClient(origin, secret)`: append its
user-free confidential code/S256 client to the existing reviewed production
realm. It allows only the exact `/identity/connect/oidc-signin` callback,
profile/email scopes and no password, implicit or service-account grant.
Keycloak startup import skips existing realms; this is not client reconciliation.
Native runtime, approved-email bootstrap, actual per-user SSO, denied-user and
master-password enrollment, encrypted data/attachment/key restore and combined
teardown remain unverified until their own VM proof.
