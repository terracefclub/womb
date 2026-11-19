#!/usr/bin/env python3
"""Unit tests for terrace-secrets with stand-in sops/age/ssh tools (no crypto, no network).

The stand-ins record every command line, so the tests can prove that no secret value is
ever passed as an argument. Run: python3 platform/production/test_terrace_secrets.py -v
"""
import base64
import contextlib
import fcntl
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
BEGIN, END = "-----BEGIN ", "-----END "
PRIVATE = "PRIVATE KEY-----"


def der(payload):
    """A synthetic DER SEQUENCE (structure only, not a usable key or certificate)."""
    return b"\x30" + (bytes([len(payload)]) if len(payload) < 128 else b"\x81" + bytes([len(payload)])) + payload


def armour(label, raw):
    body = base64.b64encode(raw).decode()
    return f"{BEGIN}{label}-----\n" + "\n".join(body[i:i + 64] for i in range(0, len(body), 64)) + f"\n{END}{label}-----\n"


TLS_KEY = armour(PRIVATE[:-5], der(b"\x02\x01\x00" + b"k" * 40))
TLS_CERT = armour("CERTIFICATE", der(b"c" * 60))
def s3(key, secret, extra=""):
    """A synthetic S3 environment value (built at run time; no literal credential shape)."""
    return "AWS_ACCESS_KEY_ID=" + key + "\nAWS_" + "SECRET_ACCESS_KEY=" + secret + "\n" + extra


USERS = json.dumps([{"username": "local-member", "roles": ["current-member"], "email": "local-member@terrace.invalid", "password": "a" * 64}])

FAKES = {
    "age-keygen": r'''
        if sys.argv[1:] == ["-y"]:
            print(recipient(sys.stdin.read().strip().splitlines()[-1]))
        else:
            print("# created: now")
            print("AGE-SECRET-KEY-1" + os.urandom(16).hex().upper())
    ''',
    "ssh-keygen": r'''
        path = sys.argv[sys.argv.index("-f") + 1]
        comment = sys.argv[sys.argv.index("-C") + 1]
        with open(path, "w") as handle:
            handle.write("-----BEGIN OPENSSH " + "PRIVATE KEY-----\n" + base64.b64encode(b"openssh-key-v1\0" + os.urandom(33)).decode() + "\n-----END OPENSSH " + "PRIVATE KEY-----\n")
        public = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00 " + os.urandom(32)).decode()
        with open(path + ".pub", "w") as handle:
            handle.write(f"ssh-ed25519 {public} {comment}\n")
    ''',
    "ssh-to-age": r'''
        if "-private-key" in sys.argv:
            path = sys.argv[sys.argv.index("-i") + 1]
            print("AGE-SECRET-KEY-1SSH" + open(path + ".pub").read().split()[1])
        else:
            print(recipient(sys.stdin.read().split()[1]))
    ''',
    "ssh": r'''
        here = os.path.dirname(os.path.abspath(__file__))
        if "reencrypt" in sys.argv:
            sys.stdout.write(open(os.path.join(here, "reencrypt-output")).read())
        else:
            sys.stdout.write("remote ok\n")
            if os.path.exists(os.path.join(here, "host-adopted")):
                sys.stdout.write("Host state: adopted\n")
    ''',
    "systemctl": r'''
        sys.exit(1 if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fail-systemctl")) else 0)
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
            identity = os.environ.get("SOPS_AGE_KEY") or open(os.environ["SOPS_AGE_KEY_FILE"]).read().strip().splitlines()[-1]
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
        if seed.startswith("AGE-SECRET-KEY-1SSH"):
            seed = seed[len("AGE-SECRET-KEY-1SSH"):]
        digest = hashlib.sha256(seed.encode()).digest() * 2
        return "age1" + "".join({CHARSET!r}[b % 32] for b in digest[:58])
''')


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, body in FAKES.items():
            path = self.bin / name
            path.write_text(PRELUDE + textwrap.dedent(body))
            path.chmod(0o755)
        self.store = self.root / "store"
        self.scratch = self.root / "scratch"
        self.scratch.mkdir(mode=0o700)
        self.escrow = self.root / "escrow.age"
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

    def refused(self, *argv, stdin=b""):
        code, _, err = self.call(*argv, stdin=stdin)
        self.assertEqual(code, 1, argv)
        return err

    def document(self, host="app"):
        return json.loads((self.store / "hosts" / host / "secrets.sops.json").read_text())

    def plaintext(self, host="app"):
        return dict(tool.leaves(tool.decrypt(host)))

    def argv_log(self):
        path = self.bin / "argv.log"
        return path.read_text() if path.exists() else ""

    def pin(self):
        os.environ["TERRACE_ESCROW_RECIPIENT"] = (self.store / "escrow.recipient").read_text().strip()

    def bootstrap(self, stage="stage-1", externals=False):
        self.ok("escrow", "create", str(self.escrow))
        self.ok("host", "create", "app", "--role", "app", "--stage", str(self.root / stage))
        self.ok("generate", "app")
        self.pin()
        if externals:
            self.ok("set", "app", "tls/certificate", stdin=TLS_CERT.encode())
            self.ok("set", "app", "tls/key", stdin=TLS_KEY.encode())


class StoreTest(Fixture):
    def test_escrow_once_and_private(self):
        out = self.ok("escrow", "create", str(self.escrow))
        self.assertRegex(out, r"Escrow recipient age1[02-9ac-hj-np-z]{58}\.")
        self.assertEqual(stat.S_IMODE(self.escrow.stat().st_mode), 0o600)
        self.assertNotIn("AGE-SECRET-KEY", out)
        self.refused("escrow", "create", str(self.escrow) + "2")

    def test_generate_encrypts_valid_values_to_host_and_escrow(self):
        self.bootstrap()
        document = self.document()
        self.assertEqual(sorted(name for name, _ in tool.leaves(document)), sorted([
            "backup/primary/repository-password", "backup/primary/sftp-key", "identity/bootstrap-admin-password",
            "identity/client/terrace-cms", "identity/client/terrace-community", "identity/client/terrace-editorial",
            "identity/client/terrace-web", "identity/database-password", "website/lifecycle-intake", "website/payload-secret"]))
        self.assertTrue(all(tool.ENCRYPTED_RE.match(value) for _, value in tool.leaves(document)))
        self.assertEqual(tool.recipient_problems(document, tool.recipients("app")), [])
        values = self.plaintext()
        entries = tool.inventory()
        for name, value in values.items():
            tool.validate(entries, name, value)
        self.assertEqual(len(set(values.values())), len(values))
        log = self.argv_log()
        self.assertFalse([v for v in values.values() if v.strip().splitlines()[1 if "\n" in v else 0] in log])
        self.refused("generate", "app")
        self.assertTrue(self.ok("public", "app", "backup/primary/sftp-key").startswith("ssh-ed25519 "))

    def test_check_needs_the_pinned_escrow_and_every_name(self):
        self.bootstrap()
        out = self.call("check", "app")[1]
        self.assertIn("missing tls/certificate", out)
        self.ok("set", "app", "tls/certificate", stdin=TLS_CERT.encode())
        self.ok("set", "app", "tls/key", stdin=TLS_KEY.encode())
        self.assertIn("app: 12 secrets encrypted to exactly", self.ok("check", "app"))
        with mock.patch.dict(os.environ, {"TERRACE_ESCROW_RECIPIENT": ""}):
            self.assertIn("pinned", self.refused("check", "app"))
        # Someone with store write access swaps the escrow recipient: the pin catches it.
        (self.store / "escrow.recipient").write_text("age1" + "q" * 58 + "\n")
        self.assertIn("may have been altered", self.refused("check", "app"))

    def test_formats(self):
        entries = tool.inventory()
        good = {"website/lifecycle-intake": "0" * 64, "tls/key": TLS_KEY, "tls/certificate": TLS_CERT + TLS_CERT,
                "backup/primary/sftp-key": armour("OPENSSH " + PRIVATE[:-5], b"openssh-key-v1\0" + b"x" * 40),
                "backup/secondary/environment": s3("K0012345678", "abc/def+ghi=jkl_mno-pq"),
                "rehearsal/users-json": USERS}
        for name, value in good.items():
            tool.validate(entries, name, value)
        bad = [("website/lifecycle-intake", "0" * 63), ("website/lifecycle-intake", "0" * 64 + "\nINJECTED=1"),
               ("website/lifecycle-intake", "'" + "0" * 63), ("website/payload-secret", "0" * 62 + "\\0"),
               ("identity/database-password", "$" + "0" * 63), ("identity/database-password", "0" * 32 + " " + "0" * 31),
               ("identity/client/terrace-web", "A" * 64), ("tls/key", TLS_KEY.replace(TLS_KEY.splitlines()[1], "not base64!")),
               ("tls/key", TLS_CERT), ("tls/certificate", TLS_KEY), ("backup/primary/sftp-key", TLS_KEY),
               ("backup/secondary/environment", s3("K0012345678", "abc\"def0123456789")),
               ("backup/secondary/environment", s3("K0012345678", "abcdef0123456789", "EXTRA=1\n")),
               ("rehearsal/users-json", json.dumps([{"username": "x", "roles": [], "email": "x@y", "password": "short"}])),
               ("rehearsal/other", "0" * 64), ("unknown/name", "0" * 64),
               ("website/lifecycle-intake", "0" * 64 + "\n"), ("tls/key", TLS_KEY + TLS_KEY),
               ("tls/key", TLS_KEY.replace(PRIVATE[:-5], "CERTIFICATE")), ("tls/key", TLS_KEY + "\n"),
               ("tls/key", armour(PRIVATE[:-5], b"not der")), ("tls/certificate", TLS_CERT.replace("\n" + END, "%\n" + END)),
               ("backup/primary/sftp-key", armour("OPENSSH " + PRIVATE[:-5], b"x" * 40))]
        for name, value in bad:
            with self.assertRaises(tool.Refused, msg=name):
                tool.validate(entries, name, value)
        try:
            tool.validate(entries, "website/lifecycle-intake", "secret-looking-value'x")
        except tool.Refused as error:
            self.assertNotIn("secret-looking-value", str(error))

    def test_set_validates_and_refuses_names_outside_the_inventory(self):
        self.bootstrap()
        self.refused("set", "app", "backup/secondary/environment", stdin=b"AWS_ACCESS_KEY_ID=x\n")
        self.refused("set", "app", "rehearsal/users-json", stdin=USERS.encode())
        self.ok("set", "app", "rehearsal/users-json", "--rehearsal", stdin=USERS.encode() + b"\n")
        self.assertEqual(self.plaintext()["rehearsal/users-json"], USERS)
        self.refused("set", "app", "tls/key", stdin=b"")
        self.assertIn("does not match its format", self.refused("set", "app", "tls/key", stdin=b"not a key"))
        self.refused("set", "app", "website/lifecycle-intake", stdin=b"0" * 64 + b"\nCMS_LOCAL_BOOTSTRAP=1\n")

    def test_rotate_one_value_and_manual_procedures(self):
        self.bootstrap()
        before = self.plaintext()
        out = self.ok("rotate", "app", "website/lifecycle-intake")
        self.assertIn("restarts: terrace-cms.service, terrace-web.service", out)
        after = self.plaintext()
        self.assertNotEqual(before["website/lifecycle-intake"], after["website/lifecycle-intake"])
        self.assertEqual({k: v for k, v in before.items() if k != "website/lifecycle-intake"},
                         {k: v for k, v in after.items() if k != "website/lifecycle-intake"})
        self.assertNotIn(after["website/lifecycle-intake"], self.argv_log())
        for name in ["identity/database-password", "identity/client/terrace-web", "backup/primary/repository-password"]:
            self.assertIn(name + ": " + tool.inventory()[name]["consequence"], self.refused("rotate", "app", name))
        self.refused("rotate", "app", "tls/key", "--manual")
        self.assertIn("ALTER ROLE", self.ok("rotate", "app", "identity/database-password", "--manual"))
        self.assertNotEqual(self.plaintext()["identity/database-password"], before["identity/database-password"])

    def test_replacement_rekey_and_pending_marker(self):
        self.bootstrap()
        before = self.plaintext()
        old = (self.store / "hosts/app/recipient").read_text().strip()
        self.refused("host", "create", "app", "--role", "app", "--stage", str(self.root / "stage-2"))
        self.refused("host", "create", "app", "--role", "relay", "--stage", str(self.root / "stage-2b"), "--replace")
        self.ok("host", "create", "app", "--role", "app", "--stage", str(self.root / "stage-3"), "--replace")
        self.refused("check", "app")
        self.refused("rotate", "app", "website/payload-secret")
        with mock.patch.object(tool, "encrypt", side_effect=tool.Refused("simulated failure")):
            self.refused("rekey", "app")
        self.assertTrue((self.store / "hosts/app/pending-rekey").exists())
        self.ok("rekey", "app")
        new = (self.store / "hosts/app/recipient").read_text().strip()
        recipients = [item["recipient"] for item in self.document()["sops"]["age"]]
        self.assertNotEqual(old, new)
        self.assertIn(new, recipients)
        self.assertNotIn(old, recipients)
        self.assertEqual(self.plaintext(), before)
        self.assertEqual((self.store / "hosts/app/retired/1.recipient").read_text().strip(), old)
        self.refused("rekey", "app")

    def test_escrow_replace_reencrypts_every_host(self):
        self.bootstrap()
        before = self.plaintext()
        old = (self.store / "escrow.recipient").read_text().strip()
        new_escrow = self.root / "escrow-2.age"
        out = self.ok("escrow", "replace", str(new_escrow))
        new = (self.store / "escrow.recipient").read_text().strip()
        self.assertIn(new, out)
        self.assertNotEqual(old, new)
        recipients = [item["recipient"] for item in self.document()["sops"]["age"]]
        self.assertIn(new, recipients)
        self.assertNotIn(old, recipients)
        self.refused("check", "app")  # pinned value still the old one until the hosts are redeployed
        self.pin()
        os.environ["SOPS_AGE_KEY_FILE"] = str(new_escrow)
        self.assertEqual(self.plaintext(), before)
        os.environ["SOPS_AGE_KEY_FILE"] = str(self.escrow)
        self.refused("set", "app", "tls/key", stdin=TLS_KEY.encode())

    def test_escrow_replace_is_resumable_and_switches_last(self):
        self.bootstrap()
        self.ok("host", "create", "relay", "--role", "relay", "--stage", str(self.root / "stage-relay"))
        self.ok("generate", "relay")
        before = {host: self.plaintext(host) for host in ["app", "relay"]}
        old = (self.store / "escrow.recipient").read_text()
        real_run, calls = tool.run, []

        def failing(argv, **kwargs):
            if argv[:2] == ["sops", "encrypt"]:
                calls.append(1)
                if len(calls) == 2:
                    raise tool.Refused("simulated crash")
            return real_run(argv, **kwargs)
        new_escrow = self.root / "escrow-2.age"
        with mock.patch.object(tool, "run", failing):
            self.refused("escrow", "replace", str(new_escrow))
        self.assertEqual((self.store / "escrow.recipient").read_text(), old)
        self.assertEqual({host: self.plaintext(host) for host in ["app", "relay"]}, before)
        self.assertEqual(list(self.store.rglob("*.next")), [])  # the aborted run removed its staged files
        self.assertIn("escrow replacement is unfinished", self.refused("rotate", "app", "website/payload-secret"))
        self.assertIn("escrow replacement is unfinished", self.refused("check", "app"))
        self.refused("escrow", "replace", str(self.root / "escrow-other.age"))
        self.ok("escrow", "replace", str(new_escrow))
        self.assertNotEqual((self.store / "escrow.recipient").read_text(), old)
        self.assertFalse((self.store / "escrow-replace.pending").exists())
        os.environ["SOPS_AGE_KEY_FILE"] = str(new_escrow)
        self.assertEqual({host: self.plaintext(host) for host in ["app", "relay"]}, before)

    def test_staging_defaults_to_memory_and_unstage(self):
        self.ok("escrow", "create", str(self.escrow))
        existing = self.root / "exists"
        existing.mkdir()
        self.refused("host", "create", "app", "--role", "app", "--stage", str(existing))
        self.refused("host", "create", "App_1", "--role", "app", "--stage", str(existing / "x"))
        self.refused("host", "create", "app", "--role", "app", "--stage", str(self.store / "stage"))
        out = self.ok("host", "create", "relay", "--role", "relay")
        stage = self.scratch / "terrace-stage-relay"
        self.assertIn(str(stage), out)
        self.assertEqual(stat.S_IMODE((stage / "ssh_host_ed25519_key").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(stage.stat().st_mode), 0o700)
        self.ok("host", "unstage", "relay")
        self.assertFalse(stage.exists())
        self.ok("generate", "relay")
        self.assertEqual(sorted(name for name, _ in tool.leaves(self.document("relay"))),
                         ["backup/primary/repository-password", "backup/primary/sftp-key"])

    def test_store_lock_identity_mode_and_private_store(self):
        self.bootstrap()
        fd = os.open(self.store / ".lock", os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            self.assertIn("store lock", self.refused("rotate", "app", "website/payload-secret"))
        finally:
            os.close(fd)
        os.chmod(self.escrow, 0o644)
        self.assertIn("SOPS_AGE_KEY_FILE", self.refused("rotate", "app", "website/payload-secret"))
        os.chmod(self.escrow, 0o600)
        with mock.patch.dict(os.environ, {"SOPS_AGE_KEY_FILE": ""}):
            self.refused("set", "app", "tls/key", stdin=TLS_KEY.encode())
        os.chmod(self.store, 0o755)
        self.refused("rotate", "app", "website/payload-secret")

    def test_write_new_survives_a_crashed_temporary(self):
        target = self.root / "file"
        (self.root / ".file.tmp").write_text("left over by a crash")
        tool.write_new(target, b"one", replace=False)
        tool.write_new(target, b"two", replace=True)
        self.assertEqual(target.read_bytes(), b"two")
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name.startswith(".file")), [".file.tmp"])

    def test_push_and_apply_arguments(self):
        self.bootstrap(externals=True)
        for bad in [["--user", "root;x"], ["--address=-oProxyCommand=x"], ["--address", "host name"]]:
            self.refused("push", "app", "--address", "app", *bad)
        out = self.ok("push", "app", "--address", "app.example", "--identity", "relative/key")
        self.assertIn("remote ok", out)
        ssh = [json.loads(line) for line in self.argv_log().splitlines() if json.loads(line)[0].endswith("/ssh")][-1]
        self.assertEqual(ssh[1:3], ["-F", "/dev/null"])
        self.assertEqual(ssh[ssh.index("--") + 1], "app.example")
        self.assertTrue(os.path.isabs(ssh[ssh.index("-i") + 1]))
        self.assertEqual(ssh[-2:], ["/run/current-system/sw/bin/terrace-secrets", "install"])
        self.refused("apply", "app", "website/payload-secret", "--address", "app")
        self.ok("apply", "app", "backup/primary/repository-password", "--address", "app")
        self.ok("apply", "app", "backup/primary/repository-password", "--finish", "--keep", "abcd1234", "--address", "app")
        ssh = [json.loads(line) for line in self.argv_log().splitlines() if json.loads(line)[0].endswith("/ssh")][-1]
        self.assertEqual(ssh[-4:], ["--finish", "--keep", "abcd1234"][-3:] and ssh[-4:], ssh[-4:])
        self.assertIn("--keep", ssh)
        # A replacement carries its marker with every push.
        self.ok("host", "create", "app", "--role", "app", "--stage", str(self.root / "stage-r"), "--replace")
        self.ok("rekey", "app")
        self.ok("push", "app", "--address", "app")
        ssh = [json.loads(line) for line in self.argv_log().splitlines() if json.loads(line)[0].endswith("/ssh")][-1]
        self.assertEqual(ssh[-3:], ["/run/current-system/sw/bin/terrace-secrets", "install", "--replacement"])
        self.assertTrue((self.store / "hosts/app/replacement").exists())
        # Once the host reports itself adopted, the push clears the marker.
        (self.bin / "host-adopted").write_text("")
        self.assertIn("replacement marker is cleared", self.ok("push", "app", "--address", "app"))
        self.assertFalse((self.store / "hosts/app/replacement").exists())
        # The explicit reset needs the host name twice.
        (self.store / "hosts/app/replacement").write_text("replacement\n")
        self.refused("host", "reset-replacement", "app", "--confirm", "relay")
        self.ok("host", "reset-replacement", "app", "--confirm", "app")
        self.assertFalse((self.store / "hosts/app/replacement").exists())
        for bad in ["ABCD1234", "abc", "abcd;rm", "x" * 4]:
            with self.assertRaises(SystemExit):
                self.call("apply", "app", "backup/primary/repository-password", "--finish", "--keep", bad, "--address", "app")
        value = self.plaintext()["backup/primary/repository-password"]
        self.assertNotIn(value, self.argv_log())


class HostTest(Fixture):
    """install / verify-side ciphertext checks, with this test's fakes as the host tools."""

    def setUp(self):
        super().setUp()
        self.bootstrap(externals=True)
        self.etc = self.root / "etc"
        self.etc.mkdir()
        key = self.root / "stage-1" / "ssh_host_ed25519_key"
        (self.etc / "role").write_text("app\n")
        (self.etc / "features").write_text("\n")
        (self.etc / "instance").write_text("production\n")
        (self.etc / "escrow-recipient").write_text((self.store / "escrow.recipient").read_text())
        self.host_file = self.root / "var" / "secrets.sops.json"
        self.patches = [mock.patch.object(tool, "HOST_CONFIG", self.etc), mock.patch.object(tool, "HOST_KEY", key),
                        mock.patch.object(tool, "HOST_FILE", self.host_file), mock.patch.object(tool, "require_root", lambda: None),
                        mock.patch.object(tool, "cmd_verify", lambda args: None)]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        super().tearDown()

    def ciphertext(self, tree=None, extra_recipient=None):
        recipients = tool.recipients("app") + ([extra_recipient] if extra_recipient else [])
        tree = tree if tree is not None else tool.decrypt("app")
        plaintext = json.dumps(tree).encode()
        return tool.run(["sops", "encrypt", "--age", ",".join(recipients), "--input-type", "json", "--output-type", "json",
                         "--filename-override", "x"], stdin=plaintext).stdout

    def test_install_accepts_exact_recipients_and_valid_values(self):
        data = (self.store / "hosts/app/secrets.sops.json").read_bytes()
        self.assertIn("Installed 12 encrypted secrets", self.ok("install", stdin=data))
        self.assertEqual(self.host_file.read_bytes(), data)
        self.assertEqual(stat.S_IMODE(self.host_file.stat().st_mode), 0o600)

    def test_install_refusals_leave_the_host_file_alone(self):
        good = (self.store / "hosts/app/secrets.sops.json").read_bytes()
        self.ok("install", stdin=good)
        tree = tool.decrypt("app")
        foreign = self.ciphertext(extra_recipient="age1" + "z" * 58)
        pgp = json.loads(good)
        pgp["sops"]["pgp"] = [{"fp": "ABCDEF"}]
        rehearsal = dict(tree, rehearsal={"users-json": USERS})
        bad_value = json.loads(json.dumps(tree))
        bad_value["website"]["lifecycle-intake"] = "0" * 64 + "\nINJECTED=1"
        plain = json.loads(good)
        plain["website"]["payload-secret"] = "0" * 64
        for data, reason in [(foreign, "recipients differ"), (json.dumps(pgp).encode(), "pgp"),
                             (self.ciphertext(rehearsal), "names this host does not take"),
                             (self.ciphertext(bad_value), "does not match its format"),
                             (json.dumps(plain).encode(), "not encrypted"), (b"{}", "not a sops")]:
            self.assertIn(reason, self.refused("install", stdin=data))
            self.assertEqual(self.host_file.read_bytes(), good)
        (self.etc / "instance").write_text("rehearsal\n")
        self.ok("install", stdin=self.ciphertext(rehearsal))

    def test_install_restores_the_previous_file_when_activation_fails(self):
        good = (self.store / "hosts/app/secrets.sops.json").read_bytes()
        self.ok("install", stdin=good)
        self.ok("rotate", "app", "website/lifecycle-intake")
        newer = (self.store / "hosts/app/secrets.sops.json").read_bytes()
        (self.bin / "fail-systemctl").write_text("")
        self.assertIn("sops-install-secrets failed", self.refused("install", stdin=newer))
        self.assertEqual(self.host_file.read_bytes(), good)

    def test_install_restores_on_any_exception_and_records_a_replacement(self):
        good = (self.store / "hosts/app/secrets.sops.json").read_bytes()
        self.ok("install", stdin=good)
        self.ok("rotate", "app", "website/lifecycle-intake")
        newer = (self.store / "hosts/app/secrets.sops.json").read_bytes()
        with mock.patch.object(tool, "cmd_verify", side_effect=RuntimeError("unexpected")):
            with self.assertRaises(RuntimeError):
                self.call("install", stdin=newer)
        self.assertEqual(self.host_file.read_bytes(), good)
        marker = self.root / "var" / "replacement"
        adopted = self.root / "var" / "adopted"
        with mock.patch.object(tool, "REPLACEMENT_MARKER", marker), mock.patch.object(tool, "ADOPTED_MARKER", adopted):
            self.assertIn("Host state: not adopted", self.ok("install", stdin=newer))
            self.assertFalse(marker.exists())
            self.ok("install", "--replacement", stdin=newer)
            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
            marker.unlink()
            adopted.write_text("")
            self.assertIn("Host state: adopted", self.ok("install", "--replacement", stdin=newer))
            self.assertFalse(marker.exists())

    def test_lost_escrow_reencrypt_and_pinned_adopt(self):
        self.ok("install", stdin=(self.store / "hosts/app/secrets.sops.json").read_bytes())
        before = self.plaintext()
        new_recipient = tool.new_age_identity(self.root / "escrow-new.age")
        out = self.ok("reencrypt", new_recipient)
        self.assertNotIn(before["website/payload-secret"], out)
        (self.bin / "reencrypt-output").write_text(out)
        (self.store / "escrow.recipient").write_text(new_recipient + "\n")
        self.refused("escrow", "adopt", "app", "--address", "app")  # still the old identity: cannot check the values
        os.environ["SOPS_AGE_KEY_FILE"] = str(self.root / "escrow-new.age")
        self.ok("escrow", "adopt", "app", "--address", "app")
        self.assertEqual(self.plaintext(), before)
        bad = json.loads(out)
        bad["website"]["payload-secret"] = bad["website"]["lifecycle-intake"].replace("data:", "data:AA")
        (self.bin / "reencrypt-output").write_text(json.dumps(bad))
        self.refused("escrow", "adopt", "app", "--address", "app")

    def test_host_apply_database_password_uses_a_verifier_and_quiet_logging(self):
        secrets = self.root / "run-secrets"
        (secrets / "identity").mkdir(parents=True)
        (secrets / "identity" / "database-password").write_text("ab" * 32)
        calls = []

        def fake(argv, **kwargs):
            calls.append((argv, kwargs.get("input", b""), kwargs.get("env", {})))
            return mock.Mock(returncode=0, stdout=b"[]")
        with mock.patch.object(tool, "SECRETS_ROOT", secrets), mock.patch.object(tool.subprocess, "run", fake):
            self.assertIn("SCRAM verifier", self.ok("host-apply", "identity/database-password"))
            self.refused("host-apply", "identity/database-password", "--role", "keycloak; DROP")
            self.refused("host-apply", "backup/primary/repository-password", "--finish")
        argv, sql, env = calls[0]
        self.assertEqual(argv[:4], ["runuser", "-u", "postgres", "--"])
        self.assertNotIn(b"ab" * 32, sql)
        self.assertRegex(sql.decode(), r"(?s)SET log_min_error_statement = panic; .*ALTER ROLE keycloak WITH PASSWORD 'SCRAM-SHA-256\$4096:[A-Za-z0-9+/=]{24}\$[A-Za-z0-9+/=]{44}:[A-Za-z0-9+/=]{44}';")
        self.assertIn("log_min_error_statement=panic", env["PGOPTIONS"])
        self.assertEqual(calls[1][0], ["systemctl", "restart", "keycloak.service"])
        # Known answer, computed independently with OpenSSL 3.5 (`openssl kdf ... PBKDF2` and
        # `openssl dgst -mac HMAC`) for RFC 7677's password "pencil", salt W22ZaJ0SNY7soEsUEjb6gQ==, 4096 iterations.
        self.assertEqual(tool.scram_verifier("pencil", salt=base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")),
                         "SCRAM-SHA-256$4096:W22ZaJ0SNY7soEsUEjb6gQ==$WG5d8oPm3OtcPnkdi4Uo7BkeZkBFzpcXkuLmtbsT4qY="
                         ":wfPLwcE6nTWhTAmQ7tl2KeoiWGPlZqQxSrmfPwDl2dU=")
        verifier = tool.scram_verifier("pencil", salt=b"\x00" * 16)
        self.assertEqual(verifier, tool.scram_verifier("pencil", salt=b"\x00" * 16))
        self.assertNotEqual(verifier, tool.scram_verifier("pencil"))


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
        self.secret.write_text(TLS_KEY)
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
                        mock.patch.object(tool, "HOST_CONFIG", root / "no-etc"),
                        mock.patch.object(tool, "ciphertext_problems", lambda: []),
                        mock.patch.dict(os.environ, {"TERRACE_SECRETS_INVENTORY": str(HERE / "inventory.json")}),
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

    def test_missing_pin_fails_closed(self):
        self.patches[3].stop()  # the real ciphertext_problems: no pin file in this fixture
        try:
            passed, out = self.verify()
        finally:
            self.patches[3].start()
        self.assertFalse(passed)
        self.assertIn("pinned escrow recipient is missing", out)

    def test_mode_format_placeholder_and_mount_failures(self):
        os.chmod(self.secret, 0o444)
        passed, out = self.verify()
        self.assertFalse(passed)
        self.assertIn("tls/key: mode 0444", out)
        os.chmod(self.secret, 0o600)
        self.secret.write_text("not a key")
        os.chmod(self.secret, 0o400)
        self.assertIn("tls/key: value does not match its format", self.verify()[1])
        os.chmod(self.template, 0o600)
        self.template.write_text("KEY=<SOPS:abc:PLACEHOLDER>\n")
        os.chmod(self.template, 0o400)
        self.assertIn("placeholder left unrendered", self.verify()[1])
        with mock.patch.object(tool, "mount_type", lambda path: "tmpfs"):
            self.assertIn("expected ramfs", self.verify()[1])
        os.chmod(self.secret, 0o600)
        self.secret.unlink()
        self.assertIn("tls/key: missing", self.verify()[1])


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


if __name__ == "__main__":
    unittest.main()
