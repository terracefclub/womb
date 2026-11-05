import { expect, test } from 'bun:test'
import { approvalActions, nativeUsers, runtimeEnvironment, sameInput, parseInput, validateRuntimePolicy } from '../../platform/vaultwarden/runtime'
const input = {origin:'https://vault.terrace.invalid',issuer:'https://identity.terrace.invalid/realms/terrace',clientId:'terrace-vaultwarden' as const,clientSecret:'s'.repeat(32),adminToken:'a'.repeat(32),approvedEmails:['approved@terrace.invalid']}
const bytes=Buffer.from(JSON.stringify(input))
test('runtime binds canonical protected input to declared host and issuer',()=>{
 expect(parseInput(bytes,input.origin,input.issuer)).toEqual(input)
 expect(()=>parseInput(bytes,'https://other.terrace.invalid',input.issuer)).toThrow()
 expect(()=>parseInput(bytes,input.origin,'https://other.terrace.invalid/realms/terrace')).toThrow()
 sameInput(bytes,bytes)
 expect(()=>sameInput(bytes,Buffer.from(JSON.stringify({...input,approvedEmails:['changed@terrace.invalid']})))).toThrow()
})
test('secret environment uses hashed admin material and preserves literal punctuation',()=>{
 const env=runtimeEnvironment({...input,clientSecret:'ab$"\\'.repeat(10)},'$argon2id$bounded-test')
 expect(env).not.toContain(input.adminToken)
 expect(env).toContain('SSO_CLIENT_SECRET="ab$\\"\\\\')
 expect(()=>runtimeEnvironment(input,'plaintext')).toThrow()
})
test('idempotent complete allowlist disables without data deletion and can re-enable',()=>{
 const users=nativeUsers([{id:'11111111-1111-1111-1111-111111111111',email:input.approvedEmails[0],userEnabled:true},{id:'22222222-2222-2222-2222-222222222222',email:'removed@terrace.invalid',userEnabled:true}])
 expect(approvalActions(users,input.approvedEmails)).toEqual({invite:[],enable:[],disable:[users[1]!.id]})
 expect(approvalActions([{...users[0]!,userEnabled:false},{...users[1]!,userEnabled:false}],input.approvedEmails)).toEqual({invite:[],enable:[users[0]!.id],disable:[]})
 expect(approvalActions([],input.approvedEmails).invite).toEqual(input.approvedEmails)
})
for (const bad of [null,{},[{id:'not-an-id',email:'test',userEnabled:true}],[{id:'11111111-1111-1111-1111-111111111111',email:'a@b.invalid',userEnabled:'true'}]]) test('reject malformed native account response '+JSON.stringify(bad),()=>expect(()=>nativeUsers(bad)).toThrow())

const policy={sso_enabled:true,sso_only:true,sso_pkce:true,sso_signups_allowed:false,sso_signups_match_email:true,sso_allow_unknown_email_verification:false,sso_debug_tokens:false,signups_allowed:false,signups_domains_whitelist:'',invitations_allowed:false,use_sendmail:false,smtp_host:null,push_enabled:false,disable_icon_download:true,disable_admin_token:false,database_max_conns:6,sso_audience_trusted:null,sso_auth_only_not_session:false}
test('native readback positive and individual unsafe overrides',()=>{
 validateRuntimePolicy(policy)
 for (const [key,value] of Object.entries(policy)) expect(()=>validateRuntimePolicy({...policy,[key]:typeof value==='boolean'?!value:'untrusted'})).toThrow()
})

test('CLI failures suppress supplied path values and raw parser errors',()=>{
 const sentinel='private-input-canary-never-print'
 const child=Bun.spawnSync([process.execPath,'--no-env-file','platform/vaultwarden/runtime.ts','bootstrap',sentinel],{cwd:import.meta.dir+'/../..',stdout:'pipe',stderr:'pipe'})
 expect(child.exitCode).toBe(1)
 expect(child.stdout.toString()).toBe('')
 expect(child.stderr.toString()).toBe('Vaultwarden private runtime operation rejected; ingress remains closed.\n')
 expect(child.stderr.toString()).not.toContain(sentinel)
})
test('read-only inspector rejects incomplete input without emitting credentials or starting a request',()=>{
 const sentinel='private-inspect-canary-never-print'
 const child=Bun.spawnSync([process.execPath,'--no-env-file','platform/vaultwarden/runtime.ts','inspect',sentinel],{cwd:import.meta.dir+'/../..',stdout:'pipe',stderr:'pipe'})
 expect(child.exitCode).toBe(1)
 expect(child.stdout.toString()).toBe('')
 expect(child.stderr.toString()).toBe('Vaultwarden private inspection rejected; diagnostics suppressed.\n')
 expect(child.stderr.toString()).not.toContain(sentinel)
})
