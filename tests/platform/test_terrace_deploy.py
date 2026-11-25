"""The operator-side deploy helper (platform/production/terrace-deploy.sh), with a stub nix.

It must refuse before signing anything unless it has a secret signing key, an SSH identity and a production system
toplevel; then sign the closure, copy it as `operator` over ssh-ng with strict host key checking, and print the
activation command without running it. The production deploy VM proof runs the real script.
Run: python3 -B tests/platform/test_terrace_deploy.py -v
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(os.environ.get("TERRACE_DEPLOY_SCRIPT", Path(__file__).resolve().parents[2] / "platform/production/terrace-deploy.sh"))
BASH = shutil.which("bash") or "/bin/bash"
TOOLS_PATH = ":".join(sorted({os.path.dirname(shutil.which(name) or "/bin/" + name) for name in ("bash", "cat", "grep")}))
SECRET = "terrace-test-deploy-1:" + "A" * 86 + "=="
PUBLIC = "terrace-test-deploy-1:" + "A" * 43 + "="


class TerraceDeploy(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        (self.bin / "nix").write_text(f"#!{BASH}\nprintf '%s\\n' \"NIX_SSHOPTS=${{NIX_SSHOPTS:-}} $*\" >>\"$FAKE_LOG\"\nexit ${{FAKE_NIX_RC:-0}}\n")
        (self.bin / "nix").chmod(0o700)
        self.log = self.dir / "nix.log"
        self.key = self.dir / "deploy.sec"
        self.key.write_text(SECRET + "\n")
        self.identity = self.dir / "operator"
        self.identity.write_text("synthetic\n")
        self.system = self.dir / "store" / ("a" * 32 + "-nixos-system-app")
        (self.system / "bin").mkdir(parents=True)
        (self.system / "etc" / "terrace").mkdir(parents=True)
        (self.system / "bin" / "switch-to-configuration").write_text("#!/bin/sh\n")
        (self.system / "bin" / "switch-to-configuration").chmod(0o700)
        (self.system / "etc" / "terrace" / "system.json").write_text('{"schema":1,"role":"app"}\n')

    def run_helper(self, *args, rc="0"):
        env = {"PATH": f"{self.bin}:{TOOLS_PATH}", "FAKE_LOG": str(self.log), "FAKE_NIX_RC": rc}
        return subprocess.run([BASH, str(SCRIPT), *args], capture_output=True, text=True, timeout=30, env=env)

    def args(self, **override):
        values = {"--key": str(self.key), "--address": "app.example.net", "--identity": str(self.identity)} | override
        return [item for pair in values.items() for item in pair if pair[1] is not None] + [str(self.system)]

    def test_signs_copies_and_prints_the_activation(self):
        result = self.run_helper(*self.args(**{"--known-hosts": str(self.identity)}))
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text().splitlines()
        self.assertEqual(calls[0], f"NIX_SSHOPTS= --extra-experimental-features nix-command store sign --key-file {self.key} --recursive {self.system}")
        self.assertEqual(calls[1], f"NIX_SSHOPTS=-i {self.identity} -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile={self.identity}"
                                   f" --extra-experimental-features nix-command copy --to ssh-ng://operator@app.example.net {self.system}")
        self.assertEqual(len(calls), 2, "the helper never activates")
        self.assertTrue(result.stdout.startswith('Manifest: {"schema":1,"role":"app"}\n'))
        self.assertTrue(result.stdout.endswith(f"  ssh -t operator@app.example.net sudo terrace-production activate {self.system}\n"))

    def test_refusals_before_signing(self):
        self.key.with_name("deploy.pub").write_text(PUBLIC + "\n")
        cases = [
            (self.args(**{"--key": None}), 2, "usage:"),
            (self.args(**{"--address": "app;rm"}), 1, "is not a host name or IP address"),
            (self.args(**{"--key": str(self.dir / "absent")}), 1, "signing key"),
            (self.args(**{"--key": str(self.key.with_name("deploy.pub"))}), 1, "is not a Nix secret signing key"),
            (self.args(**{"--identity": str(self.dir / "absent")}), 1, "SSH identity"),
            (self.args(**{"--known-hosts": str(self.dir / "absent")}), 1, "known hosts file"),
        ]
        for args, code, needle in cases:
            result = self.run_helper(*args)
            self.assertEqual((result.returncode, needle in result.stderr), (code, True), (args, result.stderr))
        (self.system / "etc" / "terrace" / "system.json").unlink()
        result = self.run_helper(*self.args())
        self.assertIn("has no Terrace system manifest", result.stderr)
        (self.system / "bin" / "switch-to-configuration").unlink()
        result = self.run_helper(*self.args())
        self.assertIn("is not a built NixOS system", result.stderr)
        self.assertFalse(self.log.exists(), "nothing was signed or copied")

    def test_a_failed_signature_stops_before_the_copy(self):
        result = self.run_helper(*self.args(), rc="1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        self.assertNotIn("Copied", result.stdout)


if __name__ == "__main__":
    unittest.main()
