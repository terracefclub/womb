"""terrace-cms-tool's own logic (platform/cms-tool.sh), with stub id, systemctl, systemd-run and date.

The module prepends TOOL_RUNNER and TOOL_PROPERTIES to the script and builds it with writeShellApplication
(errexit, nounset, pipefail); this test does the same. The website VM proof runs the real wrapper.
Run: python3 -B tests/platform/test_cms_tool.py -v
Needs Linux: GNU `stat` and `chmod` on PATH and /dev/fd/N naming the open file (the wrapper checks its lock through the
descriptor). On macOS it is expected to fail (BSD stat; /dev/fd reports another device), as noted in docs/website-host.md.
"""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(os.environ.get("CMS_TOOL_SCRIPT", Path(__file__).resolve().parents[2] / "platform/cms-tool.sh"))
RUNNER = "/nix/store/synthetic-terrace-cms-tool-run"
# Absolute: a NixOS host has no bash under /usr/bin or /bin.
BASH = shutil.which("bash") or "/bin/bash"
# cut, tr, stat and chmod come from coreutils (GNU) on the host's PATH; the stubs below stand in for everything else.
TOOLS_PATH = ":".join(sorted({os.path.dirname(BASH), *(os.path.dirname(shutil.which(name) or "/bin/" + name) for name in ("cut", "tr", "chmod")),}))
# The real stat, called through a stub that can alter the owner (descriptor) or inode (path) it reports.
REAL_STAT = shutil.which("stat") or "/bin/stat"
PROPERTIES = ["User=terrace_cms", "EnvironmentFile=/run/secrets/rendered/terrace-website/cms.env", "ReadWritePaths=/var/lib/a /var/lib/b",
              "Environment=PATH=/nix/store/x/bin:/nix/store/y/bin"]


class CmsTool(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.calls = self.dir / "systemd-run.args"
        stubs = {
            "id": '[[ $1 == -u ]] || exit 99\necho "$FAKE_UID"\n',
            "systemctl": ('if [[ "$*" == "show terrace-cms-migrate.service -p ActiveState --value" ]]; then echo "$FAKE_STATE"\n'
                          "elif [[ \"$*\" == \"list-units --plain --no-legend --state=active,activating,deactivating terrace-cms-tool-*\" ]]; then printf '%b' \"$FAKE_UNITS\"\n"
                          'else exit 99; fi\n'),
            # The lock: refused when FAKE_LOCK_BUSY is set, as `flock -n` on a held lock; it must be fd 9.
            "flock": '[[ "$*" == "-n 9" ]] || exit 99\n[[ -z "$FAKE_LOCK_BUSY" ]]\n',
            "date": '[[ $1 == +%s%N ]] || exit 99\necho 1700000000123456789\n',
            # stat -L on the descriptor: FAKE_FD_UID replaces the owner; stat on the path: FAKE_PATH_INODE replaces the inode.
            "stat": ('out=$("$REAL_STAT" "$@") || exit $?\n'
                     'if [[ $1 == -L && -n $FAKE_FD_UID ]]; then out="$FAKE_FD_UID ${out#* }"; fi\n'
                     'if [[ $1 != -L && -n $FAKE_PATH_INODE ]]; then out="${out%% *} $FAKE_PATH_INODE"; fi\n'
                     'echo "$out"\n'),
            # Records its arguments NUL-separated, prints a line to each stream and exits with the requested status.
            "systemd-run": 'printf "%s\\0" "$@" >"$FAKE_CALLS"\necho tool-output\necho tool-error >&2\nexit "$FAKE_EXIT"\n',
        }
        for name, body in stubs.items():
            path = self.bin / name
            path.write_text(f"#!{BASH}\n" + body)
            path.chmod(0o700)
        self.lock = self.dir / "tool.lock"
        prelude = f"TOOL_RUNNER={shlex.quote(RUNNER)}\nTOOL_LOCK={shlex.quote(str(self.lock))}\nTOOL_PROPERTIES=({' '.join(shlex.quote(p) for p in PROPERTIES)})\n"
        self.wrapper = self.dir / "terrace-cms-tool"
        self.wrapper.write_text(prelude + SCRIPT.read_text())

    def run_tool(self, *args, uid="0", state="inactive", status="0", units="", busy="", fd_uid="", path_inode=""):
        env = {"PATH": f"{self.bin}:{TOOLS_PATH}", "FAKE_UID": uid, "FAKE_STATE": state, "FAKE_EXIT": status, "FAKE_CALLS": str(self.calls),
               "FAKE_UNITS": units, "FAKE_LOCK_BUSY": busy, "REAL_STAT": REAL_STAT, "FAKE_FD_UID": fd_uid, "FAKE_PATH_INODE": path_inode}
        return subprocess.run([BASH, "-euo", "pipefail", str(self.wrapper), *args], capture_output=True, text=True, timeout=30, env=env)

    def recorded(self):
        return self.calls.read_bytes().decode().split("\0")[:-1]

    def test_refuses_a_caller_that_is_not_root_before_anything_else(self):
        for args in [("roster-invite", "dry-run"), ("unknown",), ()]:
            result = self.run_tool(*args, uid="1000")
            self.assertEqual((result.returncode, result.stdout, result.stderr), (77, "", "terrace-cms-tool must run as root.\n"))
        self.assertFalse(self.calls.exists())

    def test_refuses_a_missing_or_unknown_tool_with_usage(self):
        for args in [(), ("migrate",), ("sweep",), ("start",), ("roster-invite;id",), ("../wp-import",), ("--unit=x",), ("Roster-Invite",)]:
            result = self.run_tool(*args)
            self.assertEqual(result.returncode, 64, args)
            self.assertEqual(result.stderr, "Usage: terrace-cms-tool roster-invite|wp-import ARGS...\n")
        self.assertFalse(self.calls.exists())

    def test_refuses_while_a_migration_runs_and_proceeds_otherwise(self):
        for state in ["active", "activating", "reloading", "deactivating"]:
            result = self.run_tool("wp-import", "dry-run", state=state)
            self.assertEqual((result.returncode, result.stderr), (75, "A CMS migration is in progress; run the tool after it has finished.\n"), state)
            self.assertFalse(self.calls.exists())
        for state in ["inactive", "failed"]:
            self.assertEqual(self.run_tool("wp-import", "dry-run", state=state).returncode, 0, state)

    def test_runs_the_tool_in_a_transient_unit_with_the_properties_and_arguments_unchanged(self):
        args = ["apply", "--file", "/var/lib/terrace-cms-operator/roster file.csv", "--confirm-production", "", "--unit=evil", "-p", "User=root", "--", "$(id)", "a\nb"]
        result = self.run_tool("roster-invite", *args)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "tool-output\n", "tool-error\n"))
        recorded = self.recorded()
        separator = recorded.index("--")
        self.assertEqual(recorded[separator + 1:], [RUNNER, "roster-invite", *args])
        options = recorded[:separator]
        self.assertEqual(options[:7], ["--quiet", "--wait", "--pipe", "--collect", "--service-type=exec", "--unit=terrace-cms-tool-roster-invite-1700000000123456789",
                                       "--description=Terrace CMS operator tool (roster-invite)"])
        self.assertEqual(options[7:], [item for prop in PROPERTIES for item in ("-p", prop)])

    def test_the_lock_file_is_owner_only_and_never_replaced(self):
        self.assertEqual(self.run_tool("wp-import", "dry-run").returncode, 0)
        self.assertEqual(self.lock.stat().st_mode & 0o777, 0o600)
        kept = self.lock.stat().st_ino
        self.assertEqual(self.run_tool("wp-import", "dry-run").returncode, 0)
        self.assertEqual(self.lock.stat().st_ino, kept)  # a correct lock file is reused
        # An older wrapper's world-readable file: the same inode is locked and narrowed to 0600 under the lock, never
        # removed, so two wrappers that both found it nonconforming still lock one file (trunk's script replaced it).
        for wide in [0o644, 0o666, 0o640, 0o700]:
            self.lock.write_text("left by an older wrapper\n")
            self.lock.chmod(wide)
            result = self.run_tool("wp-import", "dry-run")
            self.assertEqual((result.returncode, result.stderr), (0, "tool-error\n"), oct(wide))
            self.assertEqual((self.lock.stat().st_ino, self.lock.stat().st_mode & 0o777), (kept, 0o600), oct(wide))
            self.assertEqual(self.lock.read_text(), "left by an older wrapper\n")  # opened without truncation
        self.assertTrue(self.calls.exists())

    def test_a_lock_path_that_is_not_a_regular_file_is_refused_and_left_alone(self):
        target = self.dir / "elsewhere"
        target.write_text("keep")
        target.chmod(0o644)
        cases = {
            "a symbolic link": lambda: self.lock.symlink_to(target),
            "a dangling symbolic link": lambda: self.lock.symlink_to(self.dir / "missing"),
            "a directory": lambda: self.lock.mkdir(),
            "a FIFO": lambda: os.mkfifo(self.lock),
        }
        for label, make in cases.items():
            make()
            before = os.lstat(self.lock)
            result = self.run_tool("wp-import", "dry-run")
            reason = "a symbolic link" if "link" in label else "not a regular file"
            self.assertEqual((result.returncode, result.stdout), (72, ""), label)
            self.assertEqual(result.stderr, f"The operator tool lock {self.lock} is not a root-owned regular file ({reason}); nothing was run."
                                            " Inspect it, remove it by hand and run the tool again.\n", label)
            after = os.lstat(self.lock)
            self.assertEqual((after.st_ino, after.st_mode), (before.st_ino, before.st_mode), label)  # not removed, not replaced
            self.assertFalse(self.calls.exists(), label)
            self.assertFalse((self.dir / "missing").exists(), label)  # a link is never followed, not even to create its target
            if self.lock.is_dir() and not self.lock.is_symlink():
                self.lock.rmdir()
            else:
                self.lock.unlink()
        self.assertEqual((target.read_text(), target.stat().st_mode & 0o777), ("keep", 0o644))

    def test_a_lock_owned_by_another_user_or_swapped_while_opening_is_refused(self):
        self.assertEqual(self.run_tool("wp-import", "dry-run").returncode, 0)
        kept = self.lock.stat().st_ino
        self.calls.unlink()
        message = "The operator tool lock {} is not a root-owned regular file ({}); nothing was run. Inspect it, remove it by hand and run the tool again.\n"
        # The descriptor reports another owner (as a file chowned to another account would).
        self.lock.chmod(0o644)
        result = self.run_tool("wp-import", "dry-run", fd_uid="4242")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (72, "", message.format(self.lock, "owned by uid 4242")))
        self.assertEqual((self.lock.stat().st_ino, self.lock.stat().st_mode & 0o777), (kept, 0o644))  # neither replaced nor narrowed
        # The path names another inode than the opened descriptor (swapped between the type check and the open).
        result = self.run_tool("wp-import", "dry-run", path_inode="1")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (72, "", message.format(self.lock, "it changed while it was being opened")))
        self.assertEqual(self.lock.stat().st_ino, kept)
        self.assertFalse(self.calls.exists())

    def test_refuses_while_another_wrapper_holds_the_lock(self):
        result = self.run_tool("roster-invite", "dry-run", busy="1")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (73, "", "Another terrace-cms-tool run is starting or running; run the tools one at a time.\n"))
        self.assertFalse(self.calls.exists())
        self.assertTrue(self.lock.exists())

    def test_refuses_while_a_tool_unit_is_active_and_names_it(self):
        units = "terrace-cms-tool-wp-import-1 loaded active running /nix/store/x-run\\nterrace-cms-tool-roster-invite-2 loaded activating start /nix/store/x-run\\n"
        result = self.run_tool("roster-invite", "apply", units=units)
        self.assertEqual(result.returncode, 73)
        self.assertEqual(result.stderr, "An operator tool unit is still active: terrace-cms-tool-wp-import-1 terrace-cms-tool-roster-invite-2. "
                                        "Wait for it, or stop it: systemctl stop 'terrace-cms-tool-*'\n")
        self.assertFalse(self.calls.exists())
        # Checked before the migration state: a tool unit is reported even while a migration runs.
        self.assertEqual(self.run_tool("roster-invite", "apply", units=units, state="active").returncode, 73)

    def test_passes_the_tools_exit_status_through(self):
        for status in ["0", "1", "2", "64"]:
            self.assertEqual(self.run_tool("wp-import", "dry-run", status=status).returncode, int(status))

    def test_never_reads_the_environment_file(self):
        # The protected file is only named in a property; the script has no other way to reach it.
        text = SCRIPT.read_text()
        for word in ["source ", ". /", "cat ", "EnvironmentFile", "cms.env", "$TOOL_PROPERTIES "]:
            self.assertNotIn(word, text.split("\n# Exit codes")[1])


if __name__ == "__main__":
    unittest.main()
