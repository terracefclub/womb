# Test-only trust guard shared by VM proofs whose synthetic CA or certificate is built
# at build time with a random key. Such a derivation can be rebuilt with a NEW key
# under the SAME store path (for example after a partial garbage collection), so every
# client must reference that path (an /etc symlink, SSL_CERT_FILE, curl --cacert,
# NODE_EXTRA_CA_CERTS, a mount) and nothing cached may hold a copy of it (never
# security.pki, which copies it into a separately cached trust bundle).
#
# trust_consistent(machine, anchor, address, names, label) is a cheap early check that
# the chain the server actually serves verifies, for each name, against the anchor file
# the proof's clients actually use, and fails naming the cause otherwise.
{pkgs}: ''
  def trust_consistent(machine, anchor, address, names, label):
      openssl = "${pkgs.openssl.bin}/bin/openssl"
      failed = []
      for name in names:
          base = openssl + " s_client -connect " + address + " -servername " + name
          # A handshake without verification first, so an unreachable server is not reported as a trust mismatch.
          machine.wait_until_succeeds(base + " </dev/null >/dev/null 2>&1", timeout=90)
          if machine.execute(base + " -verify_hostname " + name + " -verify_return_error -CAfile " + anchor + " </dev/null >/dev/null 2>&1", timeout=30)[0] != 0:
              failed.append(name)
      if failed:
          raise AssertionError(f"test CA and trust anchor disagree ({label}): the chain served at {address} for {failed} does not verify against {anchor}")
      print(f"{label}: served chain verifies against the clients' trust anchor {anchor} for {len(names)} names")
''
