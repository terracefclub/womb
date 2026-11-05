import { expect, test } from 'bun:test'
import { isKeycloakProviderSet, keycloakProvider, providerReadbackSummary, validateInput, windmillClient } from '../../platform/windmill/policy'
import { waitForKeycloak } from '../../platform/windmill/provider'
test('strict provider readback covers delayed upstream10s invalidation polling', async () => {
  let milliseconds = 0
  const result = await waitForKeycloak(async () => ({status:200,body:JSON.stringify({oauth:milliseconds < 12000 ? [] : [{type:'keycloak'}]})}),
    () => milliseconds, async ms => { milliseconds += ms })
  expect(result.ready).toBe(true)
  expect(milliseconds).toBeGreaterThanOrEqual(12000)
  expect(milliseconds).toBeLessThanOrEqual(30000)
})
test('missing provider stays closed at the fixed deadline and request timeouts fit remaining time', async () => {
  let milliseconds = 0
  const limits: number[] = []
  const result = await waitForKeycloak(async limit => { limits.push(limit);return {status:200,body:'{"oauth":[]}'} },
    () => milliseconds, async ms => { milliseconds += ms })
  expect(result.ready).toBe(false)
  expect(milliseconds).toBe(30000)
  expect(limits.every(value => value > 0 && value <= 10000)).toBe(true)
  expect(limits.at(-1)).toBe(250)
})
for (const arrivedAt of [30000,30001]) test(`provider returned at${arrivedAt}ms cannot publish expired readiness`, async () => {
  let milliseconds = 0
  const result = await waitForKeycloak(async () => { milliseconds += arrivedAt;return {status:200,body:'{"oauth":[{"type":"keycloak"}]}'} },
    () => milliseconds, async ms => { milliseconds += ms })
  expect(result.ready).toBe(false)
})
test('failure shape diagnostics disclose only bounded counts and fixed kinds', () => {
  expect(providerReadbackSummary({oauth:[{type:'unapproved-private-name',display_name:'private-display'},
    {type:'keycloak',display_name:null},{type:'keycloak',display_name:'private-display'},
    {type:'keycloak'},{type:'keycloak',display_name:123}]})).toEqual([
      'Windmill provider oauth count: 5', 'Windmill provider Keycloak object count: 4',
      'Windmill provider display-name kind: null.', 'Windmill provider display-name kind: string.',
      'Windmill provider display-name kind: absent.', 'Windmill provider display-name kind: other.',
    ])
  expect(providerReadbackSummary('private-body')).toEqual(['Windmill provider shape: oauth is not an array.'])
  expect(providerReadbackSummary({oauth:new Array(2000).fill('private-value')})).toEqual([
    'Windmill provider oauth count: 1001','Windmill provider Keycloak object count: 0'])
})
const valid = () => ({adminPassword:'a'.repeat(32),clientSecret:'b'.repeat(32),origin:'https://windmill.terrace.invalid',issuer:'https://identity.terrace.invalid/realms/terrace',clientId:'terrace-windmill',approvedUsers:[{email:'admin@terrace.invalid',name:'Synthetic Admin',role:'admin'}]})
test('accepts the pinned CE OpenAPI provider objects, with only reviewed Keycloak enabled', () => {
  expect(isKeycloakProviderSet({oauth:[{type:'keycloak'}],saml:null})).toBe(true)
  expect(isKeycloakProviderSet({oauth:[{type:'keycloak',display_name:'Keycloak'}],auto_login:'keycloak'})).toBe(true)
})
test('provider input matches the pinned Keycloak UI realm URL and explicit display name', () => {
  const input = validateInput(valid())
  const provider = keycloakProvider(input)
  expect(provider.org).toBe(input.issuer)
  expect(provider.display_name).toBe('Keycloak')
  expect(provider.login_config).toEqual({
    auth_url: input.issuer + '/protocol/openid-connect/auth',
    token_url: input.issuer + '/protocol/openid-connect/token',
    userinfo_url: input.issuer + '/protocol/openid-connect/userinfo',
    scopes: ['openid','profile','email','offline_access'],
  })
  expect(provider).not.toHaveProperty('connect_config')
})
for (const [name, response] of [
  ['OSS strings', {oauth:['keycloak']}],
  ['absent provider', {oauth:[]}],
  ['other provider', {oauth:[{type:'github'}]}],
  ['additional provider', {oauth:[{type:'keycloak'},{type:'github'}]}],
  ['SAML enabled', {oauth:[{type:'keycloak'}],saml:'saml'}],
  ['other auto-login', {oauth:[{type:'keycloak'}],auto_login:'github'}],
  ['unknown metadata', {oauth:[{type:'keycloak',unexpected:'value'}]}],
  ['unbounded display', {oauth:[{type:'keycloak',display_name:'x'.repeat(101)}]}],
  ['control in display', {oauth:[{type:'keycloak',display_name:'Keycloak\n'}]}],
] as const) test(`rejects unreviewed provider metadata: ${name}`, () => expect(isKeycloakProviderSet(response)).toBe(false))
test('reviewed client is confidential code/PKCE only with exact callback and no seeded users', () => {
  const client = windmillClient(valid().origin, valid().clientSecret)
  expect(client.redirectUris).toEqual(['https://windmill.terrace.invalid/user/login_callback/keycloak'])
  expect(client.attributes).toEqual({'pkce.code.challenge.method':'S256'})
  expect(client.publicClient).toBe(false)
  expect(client.directAccessGrantsEnabled).toBe(false)
  expect(client.implicitFlowEnabled).toBe(false)
  expect(client.serviceAccountsEnabled).toBe(false)
  expect(client).not.toHaveProperty('users')
})
test('valid owner-reviewed runtime policy is accepted without filesystem or transport', () => {
  const input = valid()
  expect(validateInput(input)).toBe(input)
})
for (const [name, mutate] of [
  ['unknown field', (v:any)=>v.skipBootstrap=true],
  ['weak secret', (v:any)=>v.adminPassword='short'],
  ['same secrets', (v:any)=>v.clientSecret=v.adminPassword],
  ['control in secret', (v:any)=>v.clientSecret='b'.repeat(32)+'\n'],
  ['HTTP origin', (v:any)=>v.origin='http://windmill.terrace.invalid'],
  ['origin path', (v:any)=>v.origin+='/' ],
  ['IP origin', (v:any)=>v.origin='https://127.0.0.1'],
  ['issuer query', (v:any)=>v.issuer+='?private=value'],
  ['wrong client', (v:any)=>v.clientId='other'],
  ['duplicate account', (v:any)=>v.approvedUsers.push({...v.approvedUsers[0]})],
  ['factory account', (v:any)=>v.approvedUsers[0].email='admin@windmill.dev'],
  ['no individual admin', (v:any)=>v.approvedUsers[0].role='operator'],
  ['invalid role', (v:any)=>v.approvedUsers[0].role='superuser'],
] as const) {
  test(`fails closed on ${name} without input values in errors`, () => {
    const input=valid(); mutate(input)
    expect(()=>validateInput(input)).toThrow('Windmill bootstrap rejected.')
  })
}
