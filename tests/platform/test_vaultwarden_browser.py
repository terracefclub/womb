"""Synthetic private-file/CLI guards; no browser or network is started."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).parents[2] / 'platform/vaultwarden/browser.py'
spec = importlib.util.spec_from_file_location('vault_browser', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PrivateBrowser(unittest.TestCase):
    def empty_facts(self):
        return {'refreshRequests':0,'profileStatus':None,'nativeRefreshStatus':None,'nativeRefreshDenialMatches':None,'tokenSecondsRemaining':None,'refreshTokenPresent':None,'profileEmailMatches':None,'profileExpectedIdMatches':None,'tokenSubjectMatchesProfile':None}

    def test_network_summary_keeps_only_bounded_numeric_fields(self):
        value={'validatedOidcRequests':1,'blockedRequests':2,'pageErrors':3,'tokenStatus':200,'accountStatus':400,'token':'private-token','url':'https://private.invalid/?code=private-code'}
        summary=module.network_summary(value)
        self.assertEqual(summary,{'validatedOidcRequests':1,'blockedRequests':2,'pageErrors':3,'tokenStatus':200,'accountStatus':400,**self.empty_facts()})
        self.assertNotIn('private',json.dumps(summary))

    def test_network_summary_rejects_text_booleans_and_unbounded_values(self):
        self.assertEqual(module.network_summary({'validatedOidcRequests':True,'blockedRequests':10001,'pageErrors':-1,'tokenStatus':'private-value','accountStatus':600,'profileStatus':True,'tokenSecondsRemaining':86401,'refreshTokenPresent':'private-value','profileEmailMatches':1}),{'validatedOidcRequests':0,'blockedRequests':0,'pageErrors':0,'tokenStatus':None,'accountStatus':None,**self.empty_facts()})

    def test_profile_witnesses_keep_only_equality_not_identity_values(self):
        claims={'exp':1234567890,'sub':'private-uuid'}
        payload=base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        token='synthetic.'+payload+'.not-a-signature'
        facts=module.profile_facts(token,{'id':'private-uuid','email':'private-email'},'private-email','private-uuid')
        self.assertEqual(facts,{'profileEmailMatches':True,'profileExpectedIdMatches':True,'tokenSubjectMatchesProfile':True})
        self.assertNotIn('private',json.dumps(facts))
        self.assertEqual(module.profile_facts(token,{'id':'other','email':'other'},'expected','expected'),{key:False for key in facts})
        self.assertEqual(module.profile_facts(token,None,'expected',None),{'profileEmailMatches':False,'profileExpectedIdMatches':None,'tokenSubjectMatchesProfile':False})

    def test_lifetime_status_and_refresh_witnesses_remain_bounded(self):
        facts=module.network_summary({'profileStatus':401,'tokenSecondsRemaining':59,'refreshTokenPresent':True,'profileExpectedIdMatches':False})
        self.assertEqual(facts['profileStatus'],401)
        self.assertEqual(facts['tokenSecondsRemaining'],59)
        self.assertTrue(facts['refreshTokenPresent'])
        self.assertFalse(facts['profileExpectedIdMatches'])
        for invalid in [True,'private-value',-86401,86401]:
            self.assertIsNone(module.network_summary({'tokenSecondsRemaining':invalid})['tokenSecondsRemaining'])

    def test_private_regular_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'fixture.json'
            p.write_text('{"synthetic":true}')
            p.chmod(0o600)
            self.assertEqual(module.private_read(p), {'synthetic': True})
            p.chmod(0o644)
            with self.assertRaises(AssertionError):
                module.private_read(p)

    def test_repeated_private_session_update_retains_permissions_and_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(module,'ROOT',Path(directory)):
                module.private_update('session.json',{'synthetic':1})
                module.private_update('session.json',{'synthetic':2})
                self.assertEqual(module.private_read(Path(directory)/'session.json'),{'synthetic':2})
                self.assertEqual([p.name for p in Path(directory).iterdir()],['session.json'])

    def test_oversized_symlink_and_fifo_reject(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'fixture.json'
            p.write_bytes(b'x' * 131073)
            p.chmod(0o600)
            with self.assertRaises(AssertionError):
                module.private_read(p)
            link = Path(directory) / 'link'
            link.symlink_to(p)
            with self.assertRaises(OSError):
                module.private_read(link)
            fifo = Path(directory) / 'fifo'
            os.mkfifo(fifo, 0o600)
            with self.assertRaises(AssertionError):
                module.private_read(fifo)

    def test_expiry_reader_is_only_a_margin_check(self):
        payload = base64.urlsafe_b64encode(json.dumps({'exp':1234567890}).encode()).decode().rstrip('=')
        self.assertEqual(module.jwt_exp('synthetic.' + payload + '.not-a-signature'), 1234567890)
        payload = base64.urlsafe_b64encode(json.dumps({'exp':'not-an-integer'}).encode()).decode().rstrip('=')
        with self.assertRaises(AssertionError):
            module.jwt_exp('synthetic.' + payload + '.not-a-signature')

    def test_exact_native_ownership_denial_is_not_authentication_or_missing_item(self):
        message='Cipher is not owned by user'
        body={'message':message,'validationErrors':{'':[message]},'errorModel':{'message':message,'object':'error'},'error':'','error_description':'','exceptionMessage':None,'exceptionStackTrace':None,'innerExceptionMessage':None,'object':'error'}
        module.require_cipher_denial((400,body))
        for status in [200,401,403,404,500]:
            with self.assertRaises(AssertionError):
                module.require_cipher_denial((status,body))
        with self.assertRaises(AssertionError):
            module.require_cipher_denial((400,{**body,'message':"Cipher doesn't exist"}))
        with self.assertRaises(AssertionError):
            module.require_cipher_denial((400,{**body,'data':'unexpected-private-item'}))

    def test_attachment_receipt_excludes_download_token_and_encrypted_key(self):
        digest='a'*64
        attachment={'id':'b'*20,'fileName':'2.synthetic-encrypted-name','key':'private-encrypted-key','url':'https://private.invalid/?token=private-value'}
        value=module.attachment_record({'id':'cipher','attachments':[attachment]},module.ATTACHMENT_NAME,digest)
        self.assertEqual(value,{'id':'b'*20,'sha256':digest})
        self.assertNotIn('private',json.dumps(value))
        for change in [{'id':'../escape'},{'fileName':module.ATTACHMENT_NAME},{'key':''}]:
            with self.assertRaises(AssertionError):
                module.attachment_record({'id':'cipher','attachments':[{**attachment,**change}]},module.ATTACHMENT_NAME,digest)

    def test_exact_refresh_denial_requires_pinned_status_and_error_shape(self):
        module.require_refresh_denial((400,{'error':'invalid_grant'}))
        for value in [(200,{'error':'invalid_grant'}),(401,{'error':'invalid_grant'}),(400,{'error':'private-error'}),(400,{})]:
            with self.assertRaises(AssertionError):
                module.require_refresh_denial(value)

    def test_invalid_native_path_never_connects(self):
        with self.assertRaises(AssertionError):
            module.native('/api/\r\nprivate-header', 'synthetic-not-a-token')

    def test_cli_suppresses_supplied_values_and_raw_exceptions(self):
        value = 'untrusted-private-value'
        result = subprocess.run([sys.executable, str(SOURCE), value], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, 'Vaultwarden browser check failed at private fixture validation; private diagnostics suppressed.\n')
        self.assertNotIn(value, result.stderr)


if __name__ == '__main__':
    unittest.main()
