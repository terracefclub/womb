# Test-only trust guard shared by VM proofs whose synthetic CA or certificate is built
# at build time with a random key. Such a derivation can be rebuilt with a NEW key
# under the SAME store path (for example after a partial garbage collection), so every
# client must reference that path (an /etc symlink, SSL_CERT_FILE, curl --cacert,
# NODE_EXTRA_CA_CERTS, a mount, or a bundle assembled at run time from it) and nothing
# cached may hold a copy of it (never security.pki, which copies it into a separately
# cached trust bundle).
#
# trust_consistent(machine, anchor, address, names, label) is a cheap early check that
# the chain the server actually serves verifies, for each name, against the anchor file
# the proof's clients actually use, and fails naming the cause otherwise. It verifies
# against that file only: -no-CApath and -no-CAstore stop OpenSSL from also consulting
# the default system store (an explicit -CAfile already replaces the default file), so
# the guard cannot pass through a system bundle. OpenSSL's verify reason is kept in the
# failure message.
{pkgs}: ''
  def trust_consistent(machine, anchor, address, names, label):
      openssl = "${pkgs.openssl.bin}/bin/openssl"
      failed = {}
      for name in names:
          base = openssl + " s_client -connect " + address + " -servername " + name + " -no-CApath -no-CAstore"
          # A handshake without verification first, so an unreachable server is not reported as a trust mismatch.
          machine.wait_until_succeeds(base + " </dev/null >/dev/null 2>&1", timeout=90)
          status, output = machine.execute("out=$(" + base + " -verify_hostname " + name + " -verify_return_error -CAfile " + anchor
                                           + " </dev/null 2>&1) && rc=0 || rc=$?; printf '%s\\n' \"$out\" | grep -E -m 2 'verify error|Verify return code' || true; exit $rc", timeout=30)
          if status != 0:
              failed[name] = " / ".join(line.strip() for line in output.splitlines() if line.strip())[:300] or "no verify reason printed"
      if failed:
          raise AssertionError(f"test CA and trust anchor disagree ({label}): the chain served at {address} does not verify against {anchor}: {failed}")
      print(f"{label}: served chain verifies against the clients' trust anchor {anchor} for {len(names)} names")
''
