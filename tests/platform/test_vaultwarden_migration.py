"""Synthetic exact-unit ownership and inspection failures; no services started."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('vault_migration', Path(__file__).parents[2] / 'platform/vaultwarden/migration-stop.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class OwnedUnit(unittest.TestCase):
    def run_state(self, states, group):
        calls = []
        def run(arguments):
            calls.append(arguments)
            self.assertEqual(arguments[2], module.UNIT)
            if arguments[1] == 'show':
                return states.pop(0)
            return ''
        return module.stop_owned(run, group), calls

    def test_failed_start_is_reset_only_after_stop_and_zero_pids(self):
        failed = 'ActiveState=failed\nMainPID=0\nControlPID=0\nExecMainStatus=1\nResult=exit-code'
        inactive = 'ActiveState=inactive\nMainPID=0\nControlPID=0\nResult=success'
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'cgroup.procs').write_text('')
            result, calls = self.run_state([failed, failed, inactive], Path(directory))
        self.assertEqual(result['original']['ExecMainStatus'], '1')
        self.assertEqual([c[1] for c in calls], ['show', 'stop', 'show', 'reset-failed', 'show'])
        self.assertNotIn('kill', [c[1] for c in calls])

    def test_stale_numeric_owner_never_signalled_and_live_unit_rejects(self):
        active = 'ActiveState=active\nMainPID=999999\nControlPID=0'
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                self.run_state([active, active], Path(directory))

    def test_nonempty_cgroup_and_inspection_error_reject(self):
        inactive = 'ActiveState=inactive\nMainPID=0\nControlPID=0'
        with tempfile.TemporaryDirectory() as directory:
            group = Path(directory)
            (group / 'cgroup.procs').write_text('123\n')
            with self.assertRaises(ValueError):
                self.run_state([inactive, inactive], group)
            (group / 'cgroup.procs').unlink()
            (group / 'cgroup.procs').mkdir()
            with self.assertRaises(IsADirectoryError):
                self.run_state([inactive, inactive], group)

    def test_cgroup_stat_error_is_not_treated_as_absent(self):
        inactive = 'ActiveState=inactive\nMainPID=0\nControlPID=0'
        with patch.object(Path, 'lstat', side_effect=PermissionError()):
            with self.assertRaises(PermissionError):
                self.run_state([inactive, inactive], Path('/private-test-unit-cgroup'))

    def test_ready_then_failed_child_can_be_cleaned_but_not_marked(self):
        active = 'ActiveState=active\nSubState=running\nMainPID=12345\nControlPID=0'
        failed = 'ActiveState=failed\nMainPID=0\nControlPID=0\nExecMainCode=1\nExecMainStatus=1\nResult=exit-code'
        inactive = 'ActiveState=inactive\nMainPID=0\nControlPID=0'
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'cgroup.procs').write_text('')
            result, _calls = self.run_state([active, failed, inactive], Path(directory))
        with self.assertRaises(ValueError):
            module.require_completed_migration(result)

    def test_only_live_ready_then_clean_or_expected_term_stop_can_be_marked(self):
        original = {'ActiveState':'active','SubState':'running','MainPID':'12345','ControlPID':'0'}
        for code, status in [('1','0'), ('2','15')]:
            module.require_completed_migration({'original':original,'afterStop':{'ActiveState':'inactive','Result':'success','ExecMainCode':code,'ExecMainStatus':status}})
        with self.assertRaises(ValueError):
            module.require_completed_migration({'original':{**original,'ActiveState':'failed'},'afterStop':{'ActiveState':'inactive','Result':'success','ExecMainCode':'1','ExecMainStatus':'0'}})

    def test_stop_failure_is_not_success(self):
        def failure(arguments):
            if arguments[1] == 'show':
                return 'ActiveState=inactive\nMainPID=0\nControlPID=0'
            raise RuntimeError()
        with self.assertRaises(RuntimeError):
            module.stop_owned(failure, Path('/not-present-test-cgroup'))


if __name__ == '__main__':
    unittest.main()
