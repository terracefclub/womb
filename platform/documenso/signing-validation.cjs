// Uses the pinned image's signing primitive, offline, before the writer starts.
const { readFileSync } = require('node:fs');
const { X509Certificate } = require('node:crypto');
const { createRequire } = require('node:module');
const { pathToFileURL } = require('node:url');
const requireApp = createRequire('/app/apps/remix/package.json');
(async () => {
  const { P12Signer } = await import(pathToFileURL(requireApp.resolve('@libpdf/core')).href);
  const signer = await P12Signer.create(readFileSync('/run/terrace-signing.p12'), process.env.NEXT_PRIVATE_SIGNING_PASSPHRASE, { buildChain: false });
  const certificate = new X509Certificate(Buffer.from(signer.certificate));
  const now = Date.now();
  if (!(Date.parse(certificate.validFrom) <= now && now <= Date.parse(certificate.validTo))) throw Error();
})().catch(() => {
  console.error('Documenso signing certificate validation failed; private diagnostics suppressed.');
  process.exitCode = 1;
});
