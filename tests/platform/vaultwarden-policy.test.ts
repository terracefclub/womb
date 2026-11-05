import {expect, test} from 'bun:test'
import {validateInput, vaultwardenClient} from '../../platform/vaultwarden/policy'

const valid = () => ({origin:'https://vault.terrace.invalid',issuer:'https://identity.terrace.invalid/realms/terrace',
  clientId:'terrace-vaultwarden',clientSecret:'a'.repeat(32),adminToken:'b'.repeat(32),approvedEmails:['approved@terrace.invalid']})
test('approved input is bounded and preserves separate operator and OIDC secrets', () => {
  const input = valid()
  expect(validateInput(input)).toBe(input)
})
test('initial user-free client uses confidential code/S256 and exact callback without alternate grants', () => {
  const client = vaultwardenClient(valid().origin,valid().clientSecret)
  expect(client.redirectUris).toEqual(['https://vault.terrace.invalid/identity/connect/oidc-signin'])
  expect(client.attributes).toEqual({'pkce.code.challenge.method':'S256'})
  expect(client.publicClient).toBe(false)
  expect(client.fullScopeAllowed).toBe(false)
  expect(client.directAccessGrantsEnabled).toBe(false)
  expect(client.implicitFlowEnabled).toBe(false)
  expect(client.serviceAccountsEnabled).toBe(false)
  expect(client).not.toHaveProperty('users')
})
for (const [name, mutate] of [
  ['unknown setting',(v:any)=>v.signupsAllowed=true],
  ['HTTP',(v:any)=>v.origin='http://vault.terrace.invalid'],
  ['IP',(v:any)=>v.origin='https://127.0.0.1'],
  ['origin path',(v:any)=>v.origin+='/'],
  ['issuer query',(v:any)=>v.issuer+='?secret=private-value'],
  ['wrong realm',(v:any)=>v.issuer='https://identity.terrace.invalid/realms/master'],
  ['wrong client',(v:any)=>v.clientId='other'],
  ['equal secrets',(v:any)=>v.adminToken=v.clientSecret],
  ['weak secret',(v:any)=>v.clientSecret='private-value'],
  ['control in secret',(v:any)=>v.clientSecret+='\n'],
  ['oversized secret',(v:any)=>v.clientSecret='a'.repeat(4097)],
  ['duplicate approval',(v:any)=>v.approvedEmails.push(v.approvedEmails[0])],
  ['empty approval',(v:any)=>v.approvedEmails=[]],
  ['unbounded approval',(v:any)=>v.approvedEmails=Array.from({length:101},(_,n)=>`user${n}@terrace.invalid`)],
  ['mixed-case email',(v:any)=>v.approvedEmails=['Owner@terrace.invalid']],
  ['domain leading hyphen',(v:any)=>v.approvedEmails=['owner@-host.invalid']],
  ['domain trailing hyphen',(v:any)=>v.approvedEmails=['owner@host-.invalid']],
  ['empty domain label',(v:any)=>v.approvedEmails=['owner@host..invalid']],
  ['oversized domain label',(v:any)=>v.approvedEmails=['owner@'+'h'.repeat(64)+'.invalid']],
  ['local leading dot',(v:any)=>v.approvedEmails=['.owner@terrace.invalid']],
  ['local trailing dot',(v:any)=>v.approvedEmails=['owner.@terrace.invalid']],
  ['local repeated dot',(v:any)=>v.approvedEmails=['first..last@terrace.invalid']],
  ['oversized local part',(v:any)=>v.approvedEmails=['o'.repeat(65)+'@terrace.invalid']],
] as const) test(`rejects ${name} with a static error`, () => {
  const input = valid();mutate(input)
  expect(()=>validateInput(input)).toThrow('Vaultwarden runtime input rejected.')
})
