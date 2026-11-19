#!/usr/bin/env python3
"""Unit tests for terrace-secrets with stand-in sops/age/ssh tools (no crypto, no network).

The stand-ins record every command line, so the tests can prove that no secret value is
ever passed as an argument. Run: python3 platform/production/test_terrace_secrets.py -v
"""
import base64
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
loader = importlib.machinery.SourceFileLoader("terrace_secrets", str(HERE / "terrace-secrets"))
spec = importlib.util.spec_from_loader("terrace_secrets", loader)
tool = importlib.util.module_from_spec(spec)
loader.exec_module(tool)

CHARSET = "023456789acdefghjklmnpqrstuvwxyz"
# Split so that no complete key armour appears in the repository.
ARMOUR = "-----BEGIN " + "PRIVATE KEY-----"

FAKES = {
    "age-keygen": r'''
        if sys.argv[1:] == ["-y"]:
            identity = sys.stdin.read().strip().splitlines()[-1]
            print(recipient(identity))
        else:
            print("# created: now")
            print("AGE-SECRET-KEY-1" + os.urandom(16).hex().upper())
    ''',
    "ssh-keygen": r'''
        path = sys.argv[sys.argv.index("-f") + 1]
        comment = sys.argv[sys.argv.index("-C") + 1]
        with open(path, "w") as handle:
            handle.write("-----BEGIN OPENSSH " + "PRIVATE KEY-----\n" + base64.b64encode(os.urandom(48)).decode() + "\n-----END OPENSSH " + "PRIVATE KEY-----\n")
        public = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00 " + os.urandom(32)).decode()
        with open(path + ".pub", "w") as handle:
            handle.write(f"ssh-ed25519 {public} {comment}\n")
    ''',
    "ssh-to-age": r'''
        print(recipient(sys.stdin.read().split()[1]))
    ''',
    "sops": r'''
        verb = sys.argv[1]
        def walk(tree, function):
            return {k: (v if k == "sops" else walk(v, function) if isinstance(v, dict) else function(v)) for k, v in tree.items()}
        if verb == "encrypt":
            recipients = sys.argv[sys.argv.index("--age") + 1].split(",")
            tree = json.load(sys.stdin)
            out = walk(tree, lambda v: "ENC[AES256_GCM,data:" + base64.b64encode(v.encode()).decode() + ",iv:aXY=,tag:dGFn,type:str]")
            out["sops"] = {"age": [{"recipient": r, "enc": "x"} for r in recipients], "version": "fake"}
            print(json.dumps(out))
        elif verb == "decrypt":
            document = json.load(open(sys.argv[-1]))
            identity = open(os.environ["SOPS_AGE_KEY_FILE"]).read().strip().splitlines()[-1]
            if recipient(identity) not in [item["recipient"] for item in document["sops"]["age"]]:
                sys.exit(128)
            out = walk(document, lambda v: base64.b64decode(v.split("data:")[1].split(",")[0]).decode())
            del out["sops"]
            print(json.dumps(out))
        else:
            sys.exit(2)
    ''',
}

PRELUDE = textwrap.dedent(f'''\
    #!{sys.executable}
    import base64, hashlib, json, os, sys
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "argv.log"), "a") as log:
        log.write(json.dumps(sys.argv) + "\\n")
    def recipient(seed):
        digest = hashlib.sha256(seed.encode()).digest() * 2
        return "age1" + "".join({CHARSET!r}[b % 32] for b in digest[:58])
''')


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        for name, body in FAKES.items():
            path = self.bin / name
            path.write_text(PRELUDE + textwrap.dedent(body))
            path.chmod(0o755)
        self.store = root / "store"
        self.scratch = root / "scratch"
        self.scratch.mkdir(mode=0o700)
        self.escrow = root / "escrow.age"
        self.env = mock.patch.dict(os.environ, {
            "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "TERRACE_SECRETS_STORE": str(self.store),
            "TERRACE_SECRETS_SCRATCH": str(self.scratch),
            "TERRACE_SECRETS_INVENTORY": str(HERE / "inventory.json"),
            "SOPS_AGE_KEY_FILE": str(self.escrow),
        })
        self.env.start()
        self.memory = mock.patch.object(tool, "memory_filesystem", lambda path: True)
        self.memory.start()

    def tearDown(self):
        self.memory.stop()
        self.env.stop()
        self.temp.cleanup()

    def call(self, *argv, stdin=b""):
        out, err = io.StringIO(), io.StringIO()
        fake_stdin = io.TextIOWrapper(io.BytesIO(stdin))
        with mock.patch.object(sys, "stdin", fake_stdin), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv, stdin=b""):
        code, out, err = self.call(*argv, stdin=stdin)
        self.assertEqual(code, 0, err)
        return out

    def document(self, host="app"):
        return json.loads((self.store / "hosts" / host / "secrets.sops.json").read_text())

    def plaintext(self, host="app"):
        return dict(tool.leaves(tool.decrypt(host)))

    def argv_log(self):
        path = self.bin / "argv.log"
        return path.read_text() if path.exists() else ""

    def bootstrap(self, stage="stage-1"):
        self.ok("escrow", "create", str(self.escrow))
        self.ok("host", "create", "app", "--role", "app", "--stage", str(Path(self.temp.name) / stage))
        self.ok("generate", "app")


class StoreTest(Fixture):
    def test_escrow_once_and_private(self):
        out = self.ok("escrow", "create", str(self.escrow))
        self.assertRegex(out, r"Escrow recipient age1[02-9ac-hj-np-z]{58}\.")
        self.assertEqual(stat.S_IMODE(self.escrow.stat().st_mode), 0o600)
        self.assertNotIn("AGE-SECRET-KEY", out)
        self.assertEqual(self.call("escrow", "create", str(self.escrow) + "2")[0], 1)

    def test_generate_encrypts_every_generated_secret_to_host_and_escrow(self):
        self.bootstrap()
        document = self.document()
        names = sorted(name for name, _ in tool.leaves(document))
        self.assertEqual(names, sorted([
            "backup/primary/repository-password", "backup/primary/sftp-key", "identity/bootstrap-admin-password",
            "identity/client/terrace-cms", "identity/client/terrace-community", "identity/client/terrace-editorial",
            "identity/client/terrace-web", "identity/database-password", "website/lifecycle-intake", "website/payload-secret"]))
        self.assertTrue(all(tool.ENCRYPTED_RE.match(value) for _, value in tool.leaves(document)))
        self.assertEqual(sorted(item["recipient"] for item in document["sops"]["age"]), sorted(tool.recipients("app")))
        values = self.plaintext()
        self.assertEqual(len(set(values.values())), len(values))
        self.assertTrue(all(len(value) >= 64 for value in values.values()))
        log = self.argv_log()
        self.assertFalse([value for value in values.values() if value.strip().splitlines()[1 if "\n" in value else 0] in log])
        self.assertEqual(self.call("generate", "app")[0], 1)
        public = self.ok("public", "app", "backup/primary/sftp-key")
        self.assertTrue(public.startswith("ssh-ed25519 "))

    def test_check_names_missing_externals_then_passes(self):
        self.bootstrap()
        code, out, _ = self.call("check", "app")
        self.assertEqual(code, 1)
        self.assertIn("missing tls/certificate", out)
        self.assertIn("missing tls/key", out)
        self.ok("set", "app", "tls/certificate", stdin=b"-----BEGIN CERTIFICATE-----\nMIIsynthetic\n-----END CERTIFICATE-----\n")
        self.ok("set", "app", "tls/key", stdin=(ARMOUR + "\nMIIsyntheticKey\n-----END " + "PRIVATE KEY-----\n").encode())
        self.assertIn("app: 12 secrets encrypted", self.ok("check", "app"))
        self.assertTrue(self.plaintext()["tls/key"].startswith(ARMOUR))

    def test_set_refuses_names_outside_the_host_inventory(self):
        self.bootstrap()
        self.assertEqual(self.call("set", "app", "backup/secondary/environment", stdin=b"AWS_ACCESS_KEY_ID=x\n")[0], 1)
        self.assertEqual(self.call("set", "app", "rehearsal/users-json", stdin=b"[]")[0], 1)
        self.ok("set", "app", "rehearsal/users-json", "--rehearsal", stdin=b"[]\n")
        self.assertEqual(self.plaintext()["rehearsal/users-json"], "[]")
        self.assertEqual(self.call("check", "app")[0], 1)
        self.assertEqual(self.call("set", "app", "tls/key", stdin=b"")[0], 1)

    def test_rotate_changes_only_one_value_and_refuses_manual_consumers(self):
        self.bootstrap()
        before = self.plaintext()
        out = self.ok("rotate", "app", "website/lifecycle-intake")
        self.assertIn("restarts: terrace-cms.service, terrace-web.service", out)
        after = self.plaintext()
        self.assertNotEqual(before["website/lifecycle-intake"], after["website/lifecycle-intake"])
        self.assertEqual({k: v for k, v in before.items() if k != "website/lifecycle-intake"},
                         {k: v for k, v in after.items() if k != "website/lifecycle-intake"})
        self.assertNotIn(after["website/lifecycle-intake"], self.argv_log())
        for name in ["identity/database-password", "identity/client/terrace-web", "tls/key", "backup/primary/repository-password"]:
            self.assertEqual(self.call("rotate", "app", name)[0], 1, name)

    def test_replacement_requires_rekey_and_keeps_values(self):
        self.bootstrap()
        before = self.plaintext()
        old = (self.store / "hosts/app/recipient").read_text().strip()
        self.assertEqual(self.call("host", "create", "app", "--role", "app", "--stage", str(Path(self.temp.name) / "stage-2"))[0], 1)
        self.assertEqual(self.call("host", "create", "app", "--role", "relay", "--stage", str(Path(self.temp.name) / "stage-2"), "--replace")[0], 1)
        self.ok("host", "create", "app", "--role", "app", "--stage", str(Path(self.temp.name) / "stage-3"), "--replace")
        self.assertEqual(self.call("check", "app")[0], 1)
        self.assertEqual(self.call("rotate", "app", "website/payload-secret")[0], 1)
        self.ok("rekey", "app")
        new = (self.store / "hosts/app/recipient").read_text().strip()
        recipients = [item["recipient"] for item in self.document()["sops"]["age"]]
        self.assertNotEqual(old, new)
        self.assertIn(new, recipients)
        self.assertNotIn(old, recipients)
        self.assertEqual(self.plaintext(), before)
        self.assertEqual((self.store / "hosts/app/retired/1.recipient").read_text().strip(), old)
        self.assertEqual(self.call("rekey", "app")[0], 1)

    def test_stage_directory_and_host_names(self):
        self.ok("escrow", "create", str(self.escrow))
        existing = Path(self.temp.name) / "exists"
        existing.mkdir()
        self.assertEqual(self.call("host", "create", "app", "--role", "app", "--stage", str(existing))[0], 1)
        self.assertEqual(self.call("host", "create", "App_1", "--role", "app", "--stage", str(existing / "x"))[0], 1)
        stage = Path(self.temp.name) / "stage"
        self.ok("host", "create", "relay", "--role", "relay", "--stage", str(stage))
        self.assertEqual(stat.S_IMODE((stage / "ssh_host_ed25519_key").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(stage.stat().st_mode), 0o700)
        self.ok("generate", "relay")
        self.assertEqual(sorted(name for name, _ in tool.leaves(self.document("relay"))),
                         ["backup/primary/repository-password", "backup/primary/sftp-key"])

    def test_store_must_be_private(self):
        self.store.mkdir(mode=0o755)
        os.chmod(self.store, 0o755)
        self.assertEqual(self.call("escrow", "create", str(self.escrow))[0], 1)

    def test_decrypt_needs_an_identity(self):
        self.bootstrap()
        with mock.patch.dict(os.environ, {"SOPS_AGE_KEY_FILE": ""}):
            self.assertEqual(self.call("set", "app", "tls/key", stdin=b"x" * 40)[0], 1)


class GuardTest(unittest.TestCase):
    def test_argv_guard(self):
        with self.assertRaises(tool.Refused):
            tool.guard_argv(["sops", "set", "file", "secret-value-1234"], ["secret-value-1234"])
        tool.guard_argv(["sops", "encrypt"], ["secret-value-1234"])

    def test_scratch_refuses_disk(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"TERRACE_SECRETS_SCRATCH": directory}):
            with mock.patch.object(tool, "memory_filesystem", lambda path: False):
                with self.assertRaises(tool.Refused):
                    with tool.Scratch():
                        pass

    @unittest.skipUnless(sys.platform == "linux" and os.path.isdir("/dev/shm"), "Linux memory filesystem")
    def test_memory_filesystem_on_linux(self):
        self.assertTrue(tool.memory_filesystem("/dev/shm"))
        self.assertFalse(tool.memory_filesystem("/"))

    def ciphertext(self, values, recipients):
        document = {"a": {"b": values[0]}, "c": values[1], "sops": {"age": [{"recipient": r} for r in recipients]}}
        return json.dumps(document).encode()

    def test_install_validation(self):
        enc = "ENC[AES256_GCM,data:eA==,iv:aXY=,tag:dGFn,type:str]"
        own, escrow = "age1" + "q" * 58, "age1" + "p" * 58
        self.assertEqual(tool.validate_ciphertext(self.ciphertext([enc, enc], [own, escrow]), own), 2)
        for data in [self.ciphertext([enc, "plaintext-value"], [own, escrow]), self.ciphertext([enc, enc], [escrow, "age1" + "z" * 58]),
                     self.ciphertext([enc, enc], [own]), b"not json", b"{}"]:
            with self.assertRaises(tool.Refused):
                tool.validate_ciphertext(data, own)


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.mount = root / "secrets.d"
        generation = self.mount / "1"
        (generation / "rendered" / "web").mkdir(parents=True)
        (generation / "tls").mkdir()
        self.link = root / "secrets"
        self.link.symlink_to(generation)
        self.secret = generation / "tls" / "key"
        self.secret.write_text("value")
        self.template = generation / "rendered" / "web" / "web.env"
        self.template.write_text("KEY=value\n")
        for path in [self.secret, self.template]:
            os.chmod(path, 0o400)
        ids = {"uid": os.getuid(), "gid": os.getgid(), "owner": None, "group": None}
        self.manifest = root / "manifest.json"
        self.manifest.write_text(json.dumps({
            "secrets": [{"name": "tls/key", "path": "/run/secrets/tls/key", "mode": "0400", **ids}],
            "templates": [{"name": "web/web.env", "path": "/run/secrets/rendered/web/web.env", "mode": "0400", **ids}],
        }))
        self.patches = [mock.patch.object(tool, "SECRETS_ROOT", self.link), mock.patch.object(tool, "MOUNT_ROOT", self.mount),
                        mock.patch.object(tool, "require_root", lambda: None), mock.patch.object(tool, "mount_type", lambda path: "ramfs")]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.temp.cleanup()

    def verify(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                tool.cmd_verify(mock.Mock(manifest=str(self.manifest)))
                return True, out.getvalue()
            except tool.Refused:
                return False, out.getvalue()

    def test_pass(self):
        passed, out = self.verify()
        self.assertTrue(passed, out)
        self.assertIn("PASS 1 secrets, 1 templates", out)
        self.assertNotIn("value", out.replace("values", ""))

    def test_mode_placeholder_and_mount_failures(self):
        os.chmod(self.secret, 0o444)
        passed, out = self.verify()
        self.assertFalse(passed)
        self.assertIn("tls/key: mode 0444", out)
        os.chmod(self.secret, 0o400)
        os.chmod(self.template, 0o600)
        self.template.write_text("KEY=<SOPS:abc:PLACEHOLDER>\n")
        os.chmod(self.template, 0o400)
        passed, out = self.verify()
        self.assertIn("placeholder left unrendered", out)
        with mock.patch.object(tool, "mount_type", lambda path: "tmpfs"):
            passed, out = self.verify()
        self.assertIn("expected ramfs", out)
        self.secret.unlink()
        passed, out = self.verify()
        self.assertIn("tls/key: missing", out)


if __name__ == "__main__":
    unittest.main()
