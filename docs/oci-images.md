# Inspect official OCI image pins

From a clean checkout:

```sh
direnv exec . bun platform/oci-image.ts windmill-labs/windmill 1.839.0
direnv exec . bun platform/oci-image.ts documenso/documenso v2.21.1
direnv exec . bun test tests/platform/oci-image.test.ts
```

The helper reads public metadata only from two explicitly allowed official GHCR
repositories. It selects Linux/amd64, verifies manifest and config bytes against
content digests, and rejects ambiguous platforms or mismatches. Each request has
a 30-second deadline and a response-size limit. Manifest/token redirects are
rejected; one configuration-blob hop to GHCR's exact HTTPS blob CDN is allowed,
with registry authorization removed and the returned bytes still digest-checked. The
anonymous registry bearer token stays in memory. CLI errors are fixed text and
never print tokens, input values, URLs or provider responses. Synthetic tests
inject all transport responses and make no network/provider/email call.

The result is an image manifest pin, not the Nix archive hash. Use it in a
reviewed `dockerTools.pullImage` declaration and verify that fixed-output archive
on the Linux builder under `platform/run-check` before accepting it. Never build
images on a developer's Mac. Image licenses/caps, provenance, supported features,
resource bounds and actual runtime/SSO/restore proofs remain workload-specific
review gates. This tool does not deploy, pull a runtime image or modify a registry.
