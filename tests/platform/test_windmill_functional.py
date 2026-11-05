"""Bounded private-fixture/receipt checks; real SSO/jobs belong to the VM check."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import ast
import textwrap
import unittest

SPEC = importlib.util.spec_from_file_location('windmill_functional', Path(__file__).parents[2] / 'platform/windmill-functional/functional-browser.py')
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class FunctionalReceipt(unittest.TestCase):
    def receipt(self):
        return {'workspace':'terrace','script':'f/sso-rehearsal/proof','privateVariable':'u/operator/sso-private',
                'privateDigest':'a'*64,'job':'11111111-1111-1111-1111-111111111111','resultDigest':'b'*64,
                'operator':{'username':'operator','email':'local-member@terrace.invalid'},
                'administrator':{'username':'administrator','email':'local-editorial-admin@terrace.invalid'}}

    def test_receipt_contains_stable_data_without_session_credentials(self):
        value=self.receipt()
        self.assertEqual(module.validated_receipt(value),value)
        for change in [{'token':'PRIVATE'},{'script':'https://outside.invalid/'},{'privateVariable':'../outside'},
                       {'job':'invalid'},{'resultDigest':'not-a-digest'}]:
            with self.assertRaises((AssertionError,ValueError)):
                module.validated_receipt({**value,**change})

    def test_private_receipts_are_exclusive_and_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'receipt.json'
            module.private_write(path,self.receipt())
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            self.assertEqual(module.private_read(path),self.receipt())
            with self.assertRaises(FileExistsError):module.private_write(path,{})
            path.chmod(0o644)
            with self.assertRaises(AssertionError):module.private_read(path)

    def test_symlink_fifo_and_growth_bound_reject_without_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); path=root/'file';module.private_write(path,{})
            link=root/'link';link.symlink_to(path)
            with self.assertRaises(OSError):module.private_read(link)
            import os
            fifo=root/'fifo';os.mkfifo(fifo,0o600)
            with self.assertRaises(AssertionError):module.private_read(fifo)
            path.write_bytes(b' '*131073)
            with self.assertRaises(AssertionError):module.private_read(path)

    def test_corrupt_fixture_error_does_not_expose_private_excerpt(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'credentials.json';path.write_text('{"PRIVATE_MARKER":broken');path.chmod(0o600)
            code="import importlib.util,sys;from pathlib import Path;s=importlib.util.spec_from_file_location('x',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);m.ROOT=Path(sys.argv[2]);sys.argv=['x','create'];m.entrypoint()"
            result=subprocess.run([sys.executable,'-c',code,str(SPEC.origin),directory],capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,1)
            self.assertEqual(result.stdout,'')
            self.assertEqual(result.stderr,'Windmill functional check failed at private fixture validation; private diagnostics suppressed.\n')
            self.assertNotIn('PRIVATE_MARKER',result.stderr)

    def test_final_listener_gate_rejects_inspection_errors(self):
        source=(Path(__file__).parents[2]/'platform/windmill-functional-test.nix').read_text()
        tree=ast.parse(textwrap.dedent(source.split('testScript =',1)[1].split("''",1)[1].rsplit("'';",1)[0]))
        commands=[node.args[0].value for node in ast.walk(tree) if isinstance(node,ast.Call)
                  and isinstance(node.func,ast.Attribute) and node.func.attr=='succeed' and node.args
                  and isinstance(node.args[0],ast.Constant) and isinstance(node.args[0].value,str)
                  and node.args[0].value.startswith('listeners=')]
        self.assertEqual(len(commands),1)
        for body,success in [('return 7',False),("printf 'LISTENER\\n'",False),('return 0',True)]:
            result=subprocess.run(['/bin/bash','-c','ss() { '+body+'; }; '+commands[0]],capture_output=True,text=True,timeout=3)
            self.assertEqual(result.returncode==0,success)

    def test_frontend_paths_drop_queries_fragments_private_ids_and_other_origins(self):
        self.assertEqual(module.frontend_route('https://windmill.terrace.invalid/api/oauth/list_logins?PRIVATE#PRIVATE'),'/api/oauth/list_logins')
        self.assertEqual(module.frontend_route('https://windmill.terrace.invalid/_app/immutable/entry/start.Abc_123.js?PRIVATE'),'/_app/immutable/entry/start.Abc_123.js')
        for value in ['https://PRIVATE@windmill.terrace.invalid/api/version','https://outside.invalid/PRIVATE',
                      'https://windmill.terrace.invalid:PRIVATE/api/version','https://[PRIVATE']:
            self.assertEqual(module.frontend_route(value),'other-origin')
        self.assertEqual(module.frontend_route('https://windmill.terrace.invalid/api/w/PRIVATE/variables/get_value/PRIVATE'),'other-api')
        self.assertEqual(module.frontend_route('https://windmill.terrace.invalid/_app/PRIVATE'),'other-static-asset')

    def test_frontend_positive_and_failure_metadata_stay_bounded_and_suppress_values(self):
        facts={}
        self.assertTrue(module.frontend_response(facts,'https://windmill.terrace.invalid/_app/immutable/chunks/app.ABC.js?PRIVATE',200,'script','application/javascript'))
        self.assertEqual(facts['/_app/immutable/chunks/app.ABC.js'],{'status':200,'kind':'script','mime':'application/javascript'})
        module.frontend_response(facts,'https://windmill.terrace.invalid/api/version?PRIVATE',503,'PRIVATE','PRIVATE')
        self.assertEqual(facts['/api/version'],{'status':503,'kind':'other','mime':'other'})
        retained=[module.frontend_response(facts,f'https://windmill.terrace.invalid/_app/immutable/chunks/test{index}.js',404,'script','text/html') for index in range(100)]
        self.assertEqual(len(facts),64)
        self.assertIn(False,retained)
        self.assertIn('/_app/immutable/chunks/test99.js',facts)
        self.assertEqual(module.network_error_kind('net::ERR_CONNECTION_REFUSED'),'net::ERR_CONNECTION_REFUSED')
        self.assertEqual(module.network_error_kind('PRIVATE'),'other-network-error')
        self.assertEqual(module.page_error_kind('Failed to fetch dynamically imported module: PRIVATE'),'dynamic-import')
        self.assertEqual(module.page_error_kind('PRIVATE'),'other-page-error')
        self.assertEqual(module.page_error_class('SyntaxError'),'SyntaxError')
        self.assertEqual(module.page_error_class('PRIVATE'),'other-error-class')
        self.assertNotIn('PRIVATE',str(facts))

    def test_each_login_begins_with_unobserved_auth_and_independent_frontend_facts(self):
        prior=module.auth_facts();prior.update(providerRequestObserved=True,callbackStatus=400,pkceS256=True,challengePresent=True)
        self.assertEqual(module.auth_facts(),{'providerRequestObserved':False,'callbackStatus':None,'pkceS256':None,'challengePresent':None})
        old=module.frontend_facts();old['responses']['/api/version']={'status':503}
        self.assertEqual(module.frontend_facts()['responses'],{})

    def test_completed_job_diagnostics_retain_only_booleans_not_private_payloads(self):
        owner={'email':'operator@terrace.invalid','username':'operator'}
        self.assertEqual(module.completion_facts({'success':True,'result':{'status':'synthetic-per-user-job-ok'},
            'email':owner['email'],'created_by':owner['username'],'logs':'PRIVATE'},owner),
            {'success':True,'resultMatches':True,'ownerMatches':True})
        observed=module.completion_facts({'success':'PRIVATE','result':{'error':'PRIVATE'},
            'email':'PRIVATE','created_by':'PRIVATE','token':'PRIVATE'},owner)
        self.assertEqual(observed,{'success':None,'resultMatches':False,'ownerMatches':False})
        self.assertNotIn('PRIVATE',str(observed))

    def test_native_cached_session_converges_within_bound_without_accepting_errors_or_late_denial(self):
        now=[0.0]
        def clock():return now[0]
        def pause(seconds):now[0]+=seconds
        self.assertEqual(module.wait_for_session_denial(lambda:200 if now[0]<120 else 401,clock,pause),120000)
        now[0]=0
        with self.assertRaises(AssertionError):module.wait_for_session_denial(lambda:200,clock,pause)
        self.assertEqual(now[0],130)
        with self.assertRaises(AssertionError):module.wait_for_session_denial(lambda:500,clock,pause)
        now[0]=0
        def late():now[0]=131;return 401
        with self.assertRaises(AssertionError):module.wait_for_session_denial(late,clock,pause)


if __name__=='__main__':unittest.main()
