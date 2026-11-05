import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

source = Path(__file__).parents[2] / "platform/windmill/diagnostics.py"
spec = importlib.util.spec_from_file_location("windmill_diagnostics", source)
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)
capture_spec = importlib.util.spec_from_file_location("windmill_capture", source.with_name("capture.py"))
capture = importlib.util.module_from_spec(capture_spec)
with patch.dict(sys.modules, {"diagnostics": diagnostics}):
    capture_spec.loader.exec_module(capture)


class EvidenceParser(unittest.TestCase):
    def test_transient_cleanup_accepts_only_stopped_or_proven_collected_units(self):
        nix = source.parent.parent.joinpath('windmill-test.nix').read_text()
        body = nix[nix.index('    def stop_test_blocker(unit):'):nix.index('\n    try:', nix.index('    def stop_test_blocker(unit):'))]
        class Host:
            def __init__(self, status, active, loaded, options):
                self.status, self.active, self.loaded = status, active, loaded
                self.options, self.reset = options, False
            def execute(self, _command, **_kwargs):
                return self.status, ''
            def succeed(self, command, **_kwargs):
                if command.startswith('systemctl reset-failed '):
                    if self.options.get('resetFails'):
                        raise AssertionError()
                    self.reset = True
                    return ''
                if command.startswith('test ! -d '):
                    if self.options.get('cgroupPopulated'):
                        raise AssertionError()
                    if self.options.get('cgroupInspectionFails'):
                        # Execute the actual shell expression against an owned
                        # directory and a find that emits nothing but exits1.
                        with tempfile.TemporaryDirectory() as temporary:
                            root = Path(temporary)
                            tool = root / 'find'
                            tool.write_text('#!/bin/sh\nexit 1\n')
                            tool.chmod(0o700)
                            environment = os.environ | {'PATH': str(root) + ':' + os.environ['PATH']}
                            script = command.replace('/sys/fs/cgroup/system.slice/windmill-test-db-block.service', str(root))
                            result = subprocess.run(['bash', '-c', script], env=environment, capture_output=True, timeout=3)
                            if result.returncode != 0:
                                raise AssertionError()
                    return ''
                reset = self.reset and not self.options.get('staysFailed')
                values = {'MainPID': self.options.get('mainPID', '0'), 'ControlPID': '0',
                          'LoadState': 'not-found' if reset else self.loaded,
                          'ActiveState': 'inactive' if reset else self.active, 'ExecMainStatus': '1',
                          'ControlGroup': '/system.slice/windmill-test-db-block.service' if self.options.get('cgroupPopulated') or self.options.get('cgroupInspectionFails') else ''}
                return '\n'.join(key + '=' + value for key, value in values.items())
        for status, active, loaded, options, accepts in [
            (0, 'inactive', 'loaded', {}, True), (5, 'inactive', 'not-found', {}, True),
            (5, 'active', 'not-found', {}, False), (5, 'inactive', 'loaded', {}, False),
            (1, 'inactive', 'not-found', {}, False), (0, 'active', 'loaded', {}, False),
            (0, 'failed', 'loaded', {}, True),
            (0, 'failed', 'loaded', {'mainPID':'42'}, False),
            (0, 'failed', 'loaded', {'cgroupPopulated':True}, False),
            (0, 'failed', 'loaded', {'cgroupInspectionFails':True}, False),
            (0, 'failed', 'loaded', {'resetFails':True}, False),
            (0, 'failed', 'loaded', {'staysFailed':True}, False),
        ]:
            with self.subTest(status=status, active=active, loaded=loaded, options=options), contextlib.redirect_stdout(io.StringIO()):
                namespace = {'host': Host(status, active, loaded, options)}
                exec(textwrap.dedent(body), namespace)
                stop = lambda: namespace['stop_test_blocker']('windmill-test-db-block.service')
                if accepts:
                    stop()
                else:
                    with self.assertRaises(AssertionError):
                        stop()

    def test_provider_facts_preserve_fixed_shapes_and_counts_without_values(self):
        output = diagnostics.summarize_text("Windmill provider display-name kind: null. private-name\n"
                                            "Windmill provider HTTP status: 200 private-body\n"
                                            "Windmill provider oauth count: 1\n"
                                            "Windmill provider Keycloak object count: 1\n"
                                            "Windmill provider display-name kind: private-value.")
        self.assertEqual(output, ["Windmill provider display-name kind: null.",
                                  "Windmill provider HTTP status: 200", "Windmill provider oauth count: 1",
                                  "Windmill provider Keycloak object count: 1"])

    def test_positive_bootstrap_and_completed_message_survive_journal_prefixes(self):
        messages = ["Windmill bootstrap stage: protected input validated.",
                    "Windmill approved-user bootstrap completed; factory credential rejected."]
        text = "\n".join("Nov 5 unit[123]: " + message for message in messages)
        self.assertEqual(diagnostics.summarize_text(text), messages)

    def test_uppercase_keycloak_failure_and_each_known_failure_are_preserved(self):
        for stage in diagnostics.STAGES + ["instance policy " + key for key in diagnostics.SETTINGS]:
            message = "Windmill bootstrap failed at " + stage + "; ingress remains closed. Raw diagnostics suppressed."
            self.assertIn(message, diagnostics.summarize_text(message))

    def test_unknown_stage_and_raw_sensitive_suffixes_are_never_copied(self):
        private = "synthetic-private-provider-token"
        text = "Windmill bootstrap failed at " + private + "; password=" + private
        text += "\npermission denied: " + private + " https://user:" + private + "@fixture.invalid"
        output = diagnostics.summarize_text(text)
        self.assertEqual(output, ["Observed private log category: permission denied"])
        self.assertNotIn(private, "\n".join(output))

    def test_absence_emits_no_stage_or_failure_conclusion(self):
        self.assertEqual(diagnostics.summarize_text("unrelated private response"), [])

    def test_pending_status_one_is_not_reported_as_an_exit(self):
        raw = "{ path=/private; argv[]=sensitive; pid=5; code=(null); status=1 }"
        self.assertEqual(diagnostics.post_start_completions(raw), [{"kind": "unreported"}])
        output = diagnostics.summarize_unit({"ActiveState": "activating", "SubState": "start-post", "ExecStartPost": raw})
        self.assertIn("Windmill unit ActiveState: activating", output)
        self.assertIn("Windmill post-start command 0 completion: unreported", output)
        self.assertNotIn("status 1", "\n".join(output))
        self.assertNotIn("sensitive", "\n".join(output))

    def test_completed_success_failure_and_signal_are_distinct(self):
        raw = "{ code=exited; status=0 } { code=exited; status=1 } { code=killed; status=9 }"
        self.assertEqual(diagnostics.post_start_completions(raw), [
            {"kind": "exited", "status": 0}, {"kind": "exited", "status": 1}, {"kind": "killed", "status": 9}])

    def test_selected_unit_facts_never_echo_unknown_values_or_argv(self):
        private = "synthetic-private-value"
        output = diagnostics.summarize_unit({"ActiveState": private, "SubState": private, "Result": "start-limit-hit",
                                              "MainPID": "42", "ControlPID": private, "ExecStartPost": private})
        self.assertEqual(output, ["Windmill unit Result: start-limit-hit", "Windmill unit MainPID: 42"])

    def test_numeric_resource_evidence_does_not_copy_trailing_content(self):
        self.assertEqual(diagnostics.summarize_text("Windmill resource pids.events max: 7 private-token"),
                         ["Windmill resource pids.events max: 7"])


class PrivateArtifact(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name)

    def test_raw_capture_is_private_and_never_overwrites_existing_evidence(self):
        root = self.parent / 'evidence'
        data = {'raw': 'synthetic-private-evidence'}
        capture.write_private(data, root)
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((root / 'raw.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads((root / 'raw.json').read_text()), data)
        with self.assertRaises(FileExistsError):
            capture.write_private({'raw': 'replacement'}, root)
        self.assertEqual(json.loads((root / 'raw.json').read_text()), data)

    def test_public_or_symlink_destination_rejected_without_touching_sentinel(self):
        sentinel = self.parent / 'sentinel'
        sentinel.write_text('retained')
        public = self.parent / 'public'
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        with self.assertRaises(ValueError):
            capture.write_private({}, public)
        link = self.parent / 'link'
        link.symlink_to(public, target_is_directory=True)
        with self.assertRaises(ValueError):
            capture.write_private({}, link)
        self.assertEqual(sentinel.read_text(), 'retained')
        self.assertFalse((public / 'raw.json').exists())

    def test_fifo_launcher_is_rejected_without_blocking(self):
        fifo = self.parent / 'launcher'
        os.mkfifo(fifo, 0o600)
        program = "import sys;sys.path.insert(0,sys.argv[1]);from capture import read_launcher;read_launcher(sys.argv[2])"
        result = subprocess.run([sys.executable, '-c', program, str(source.parent), str(fifo)],
                                capture_output=True, text=True, timeout=2)
        self.assertNotEqual(result.returncode, 0)

    def test_command_output_is_byte_bounded_and_marks_truncation(self):
        result = capture.command([sys.executable, '-c', "import sys;sys.stdout.write('x'*100000)"], limit=1024)
        self.assertEqual(result['exitStatus'], 0)
        self.assertEqual(len(result['stdout'].encode()), 1024)
        self.assertTrue(result['truncated'])
        self.assertFalse(result['timedOut'])

    def test_command_timeout_stops_its_child_and_suppresses_raw_arguments(self):
        result = capture.command([sys.executable, '-c', "import time;time.sleep(30)", 'synthetic-private-argument'], timeout=0.1)
        self.assertTrue(result['timedOut'])
        self.assertIsNotNone(result['exitStatus'])
        self.assertNotIn('synthetic-private-argument', json.dumps(result))


if __name__ == "__main__":
    unittest.main()
