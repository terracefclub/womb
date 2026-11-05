import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).parents[2] / 'platform/windmill/recovery.py'
spec = importlib.util.spec_from_file_location('windmill_recovery', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RecoveryReadback(unittest.TestCase):
    def test_protected_input_rejects_corrupt_public_symlink_and_fifo(self):
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            path = Path(directory) / 'bootstrap.json'
            value = {'adminPassword':'synthetic-test-input-'*3,'approvedUsers':[{'email':'admin@terrace.invalid','role':'admin'}]}
            path.write_text(json.dumps(value));path.chmod(0o400)
            self.assertEqual(module.protected_input(directory, owner=os.getuid())[1], value['approvedUsers'])
            path.chmod(0o600)
            with self.assertRaisesRegex(ValueError, '^Windmill recovery inputs rejected.$'):
                module.protected_input(directory, owner=os.getuid())
            path.write_text('{"private-canary-never-print":broken');path.chmod(0o400)
            with self.assertRaises(ValueError) as caught:
                module.protected_input(directory, owner=os.getuid())
            self.assertNotIn('private-canary', str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)
            path.unlink();path.symlink_to('/dev/null')
            with self.assertRaises(ValueError):module.protected_input(directory, owner=os.getuid())
            path.unlink();os.mkfifo(path,0o400)
            with self.assertRaises(ValueError):module.protected_input(directory, owner=os.getuid())

    def run_protocol(self, invalid=False):
        approved=[{'email':'admin@terrace.invalid','role':'admin'}]
        bodies=[b'synthetic-native-token',json.dumps({'email':'wrong@terrace.invalid' if invalid else 'admin@windmill.dev','super_admin':True,'disabled':False}).encode(),
                b'[{"id":"terrace"}]',json.dumps([{'email':'admin@terrace.invalid','super_admin':True,'disabled':False,'login_type':'keycloak'}]).encode(),b'logged out successfully']
        calls=[]
        class Response:
            status=200
            def __init__(self, body):self.body=body
            def read(self, limit):
                self.assert_limit=limit
                return self.body
        class Connection:
            def __init__(self, host, port, timeout):
                assert host=='127.0.0.1' and port==18001 and timeout==5
            def request(self, method, path, body, headers):
                calls.append((method,path))
                self.path=path
            def getresponse(self):
                if self.path=='/api/auth/logout':return Response(b'logged out successfully')
                return Response(bodies.pop(0))
            def close(self):pass
        with patch.object(module,'protected_input',return_value=('synthetic-test-input-'*3,approved)),patch.object(module.http.client,'HTTPConnection',Connection),patch('socket.socket.connect',side_effect=AssertionError('Network forbidden')):
            if invalid:
                with self.assertRaises(AssertionError):module.inspect('/unused',18001)
            else:module.inspect('/unused',18001)
        self.assertEqual(calls[0],('POST','/api/auth/login'))
        self.assertEqual(calls[-1],('POST','/api/auth/logout'))
        self.assertTrue(all(method=='GET' for method,path in calls[1:-1]))

    def test_native_reads_use_only_loopback_and_logout_own_session(self):
        self.run_protocol()

    def test_failed_account_readback_still_logs_out(self):
        self.run_protocol(invalid=True)

    def test_cli_errors_never_reflect_supplied_values(self):
        result=subprocess.run([sys.executable,str(SOURCE),'private-canary-never-print'],capture_output=True,text=True,timeout=3)
        self.assertEqual(result.returncode,1)
        self.assertEqual(result.stdout,'')
        self.assertEqual(result.stderr,'Windmill private recovery readback rejected; diagnostics suppressed.\n')


if __name__=='__main__':unittest.main()
