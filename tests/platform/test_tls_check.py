"""terrace-tls-check failure reasons (platform/production/tls-check.sh).

The fake `openssl` replays s_client output in OpenSSL 3's format; the real
OpenSSL run (when an OpenSSL 3 binary is on PATH) checks that format against a
local s_server. The TLS VM proof asserts the reasons with the pinned OpenSSL.
"""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

SCRIPT = Path(os.environ.get("TLS_CHECK_SCRIPT", Path(__file__).resolve().parents[2] / "platform/production/tls-check.sh"))
NAME = "web.terrace.test"


def run(path, env):
    # writeShellApplication runs the body with errexit, nounset and pipefail.
    return subprocess.run(["bash", "-euo", "pipefail", str(SCRIPT)], capture_output=True, text=True, timeout=60,
                          env={"PATH": path, "TLS_CHECK_MIN_DAYS": "14", **env})


class FakeOpenSSL(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.bin = Path(directory.name)
        self.output = self.bin / "s_client.out"
        fake = self.bin / "openssl"
        # s_client writes its verification and connection diagnostics to stderr.
        fake.write_text('#!/usr/bin/env bash\n[[ $1 == s_client ]] || exit 99\ncat "$FAKE_OUTPUT" >&2\nexit "$FAKE_STATUS"\n')
        fake.chmod(0o700)
        # The check bounds each handshake with coreutils timeout; here the fake answers at once and records the bound.
        self.bound = self.bin / "bound"
        stub = self.bin / "timeout"
        stub.write_text('#!/usr/bin/env bash\necho "$1" >>"$FAKE_BOUND"\nshift\nexec "$@"\n')
        stub.chmod(0o700)

    def check(self, output, status, names=NAME, **extra):
        self.output.write_text(output)
        return run(f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                   {"FAKE_OUTPUT": str(self.output), "FAKE_STATUS": str(status), "FAKE_BOUND": str(self.bound), "TLS_CHECK_NAMES": names,
                    "TLS_CHECK_TRUST": "/synthetic/roots.pem", "TLS_CHECK_CONNECT": "127.0.0.1:443", **extra})

    def test_a_subject_containing_the_phrase_is_not_taken_for_the_reason(self):
        # OpenSSL prints the subject after "depth=N "; only its own line starts with "verify error:num=".
        result = self.check("depth=0 CN=verify error:num=9:certificate is not yet valid\n"
                            "verify error:num=18:self-signed certificate\n", 1)
        self.assertEqual(result.stdout, f"{NAME}: FAIL: no verified TLS session: verify error 18: self-signed certificate\n")

    def test_the_handshake_bound_is_30_s_unless_set(self):
        self.check("CONNECTED(00000003)\n", 124)
        result = self.check("CONNECTED(00000003)\n", 124, TLS_CHECK_TIMEOUT="5")
        self.assertEqual(self.bound.read_text().split(), ["30", "5"])
        self.assertEqual(result.stdout, f"{NAME}: FAIL: no verified TLS session: no answer from 127.0.0.1:443 within 5 s\n")

    def test_a_clock_behind_the_certificate_is_named_not_an_untrusted_chain(self):
        result = self.check("Connecting to 127.0.0.1\nCONNECTED(00000003)\n"
                            "depth=0 CN=web.terrace.test\nverify error:num=9:certificate is not yet valid\n"
                            "40F7C2E5D17F0000:error:0A000086:SSL routines:tls_post_process_server_certificate:certificate verify failed:../ssl/statem/statem_clnt.c:2124:\n", 1)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, f"{NAME}: FAIL: no verified TLS session: verify error 9: certificate is not yet valid\n")

    def test_the_first_verify_error_is_the_reason(self):
        result = self.check("depth=0 CN=minica root ca 1a2b3c\nverify error:num=20:unable to get local issuer certificate\n"
                            "verify error:num=21:unable to verify the first certificate\n", 1)
        self.assertEqual(result.stdout, f"{NAME}: FAIL: no verified TLS session: verify error 20: unable to get local issuer certificate\n")

    def test_expired_and_self_signed(self):
        for number, text in [(10, "certificate has expired"), (18, "self-signed certificate")]:
            with self.subTest(number=number):
                result = self.check(f"verify error:num={number}:{text}\n", 1)
                self.assertEqual(result.stdout, f"{NAME}: FAIL: no verified TLS session: verify error {number}: {text}\n")

    def test_nothing_listening(self):
        result = self.check("Connecting to 127.0.0.1\n40C7:error:8000006F:system library:BIO_connect:Connection refused:../crypto/bio/bio_sock2.c:178:calling connect()\n"
                            "connect:errno=111\n", 1)
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stdout, rf"^{NAME}: FAIL: no verified TLS session: cannot connect to 127\.0\.0\.1:443 \(.*Connection refused.*\)\n$")

    def test_no_answer_within_the_bound(self):
        result = self.check("CONNECTED(00000003)\n", 124)
        self.assertEqual(result.stdout, f"{NAME}: FAIL: no verified TLS session: no answer from 127.0.0.1:443 within 30 s\n")

    def test_other_handshake_errors_and_no_output(self):
        result = self.check("40F7:error:0A000410:SSL routines:ssl3_read_bytes:sslv3 alert handshake failure:../ssl/record/rec_layer_s3.c:916:SSL alert number 40\n", 1)
        self.assertEqual(result.stdout, f"{NAME}: FAIL: no verified TLS session: handshake failed: sslv3 alert handshake failure (SSL routines)\n")
        result = self.check("", 1)
        self.assertEqual(result.stdout, f"{NAME}: FAIL: no verified TLS session: handshake failed, no reason reported\n")

    def test_every_name_gets_its_own_line(self):
        result = self.check("verify error:num=9:certificate is not yet valid\n", 1, names=f"{NAME} cms.terrace.test")
        self.assertEqual(result.returncode, 1)
        self.assertEqual([line.split(":", 1)[0] for line in result.stdout.splitlines()], [NAME, "cms.terrace.test"])


def openssl3():
    found = shutil.which("openssl")
    if found is None:
        return None
    version = subprocess.run([found, "version"], capture_output=True, text=True).stdout
    return found if version.startswith("OpenSSL 3") else None


@unittest.skipUnless(openssl3() and shutil.which("timeout"), "needs an OpenSSL 3 binary and coreutils timeout")
class RealOpenSSL(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)

    def openssl(self, *args):
        subprocess.run(["openssl", *args], cwd=self.dir, check=True, capture_output=True, timeout=60)

    def serve(self, cert, key):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = subprocess.Popen(["openssl", "s_server", "-accept", f"127.0.0.1:{port}", "-cert", cert, "-key", key, "-www", "-quiet"],
                                  cwd=self.dir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(server.wait, 10)
        self.addCleanup(server.terminate)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with socket.socket() as client:
                if client.connect_ex(("127.0.0.1", port)) == 0:
                    return port
            time.sleep(0.05)
        self.fail("s_server did not listen")

    def check(self, port, trust, **extra):
        return run(os.environ["PATH"], {"TLS_CHECK_NAMES": NAME, "TLS_CHECK_TRUST": trust, "TLS_CHECK_CONNECT": f"127.0.0.1:{port}", **extra})

    def test_a_subject_containing_the_phrase_does_not_become_the_reason(self):
        phrase = "verify error:num=9:certificate is not yet valid"
        self.openssl("req", "-x509", "-nodes", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-days", "30",
                     "-subj", f"/CN={phrase}", "-keyout", "leaf-key.pem", "-out", "leaf.pem",
                     "-addext", f"subjectAltName=DNS:{NAME}")
        self.openssl("req", "-x509", "-nodes", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-days", "30",
                     "-subj", "/CN=synthetic other", "-keyout", "other-key.pem", "-out", "other.pem")
        port = self.serve("leaf.pem", "leaf-key.pem")
        result = self.check(port, str(self.dir / "other.pem"))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, f"{NAME}: FAIL: no verified TLS session: verify error 18: self-signed certificate\n")

    def test_a_listener_that_never_answers_is_bounded(self):
        # Accepts the connection and never speaks TLS, as a hung server would.
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        port = listener.getsockname()[1]
        self.openssl("req", "-x509", "-nodes", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-days", "30",
                     "-subj", "/CN=synthetic ca", "-keyout", "ca-key.pem", "-out", "ca.pem")
        started = time.monotonic()
        result = self.check(port, str(self.dir / "ca.pem"), TLS_CHECK_TIMEOUT="2")
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, f"{NAME}: FAIL: no verified TLS session: no answer from 127.0.0.1:{port} within 2 s\n")

    def test_trusted_passes_and_an_untrusted_chain_names_openssls_reason(self):
        for label in ["ca", "other"]:
            self.openssl("req", "-x509", "-nodes", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-days", "30",
                         "-subj", f"/CN=synthetic {label}", "-keyout", f"{label}-key.pem", "-out", f"{label}.pem",
                         "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign")
        self.openssl("req", "-new", "-nodes", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-subj", f"/CN={NAME}",
                     "-keyout", "leaf-key.pem", "-out", "leaf.csr")
        (self.dir / "leaf.ext").write_text(f"basicConstraints=critical,CA:FALSE\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:{NAME}\n")
        self.openssl("x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca-key.pem", "-set_serial", "1",
                     "-days", "30", "-extfile", "leaf.ext", "-out", "leaf.pem")
        port = self.serve("leaf.pem", "leaf-key.pem")
        result = self.check(port, str(self.dir / "ca.pem"))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertRegex(result.stdout, rf"^{NAME}: OK: expires .*; issuer CN=synthetic ca\n$")
        result = self.check(port, str(self.dir / "other.pem"))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, f"{NAME}: FAIL: no verified TLS session: verify error 20: unable to get local issuer certificate\n")
        # 30 days left is fewer than 14 + 30: the expiry message, not a verification reason.
        days = run(os.environ["PATH"], {"TLS_CHECK_NAMES": NAME, "TLS_CHECK_TRUST": str(self.dir / "ca.pem"),
                                        "TLS_CHECK_CONNECT": f"127.0.0.1:{port}", "TLS_CHECK_MIN_DAYS": "45"})
        self.assertEqual(days.returncode, 1)
        self.assertRegex(days.stdout, rf"^{NAME}: FAIL: expires .*, fewer than 45 days left\n$")


if __name__ == "__main__":
    unittest.main()
