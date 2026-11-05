import { describe, expect, test } from 'bun:test'
import { fixtureClients } from '../../platform/website-gateway/fixture-clients'

const base = [{ clientId: 'terrace-web', secret: 'synthetic-base-only' }]
const factory = (secrets: Readonly<Record<string, string>>) => ({
  clientId: 'terrace-extra', secret: secrets.extra, protocol: 'openid-connect', enabled: true, publicClient: false, fullScopeAllowed: false,
  redirectUris: ['https://extra.terrace.invalid/auth/callback'], webOrigins: ['https://extra.terrace.invalid'],
  standardFlowEnabled: true, directAccessGrantsEnabled: false, implicitFlowEnabled: false,
  serviceAccountsEnabled: false, attributes: { 'pkce.code.challenge.method': 'S256' },
})
const options = { extraSecretNames: ['extra'], extraClientFactories: [factory] }
const rejects = (value: unknown) => expect(() => fixtureClients(base, value as typeof options)).toThrow('Synthetic fixture extension rejected.')

describe('private disposable identity client composition', () => {
  test('default retains accepted clients without adding secret material', () => {
    expect(fixtureClients(base)).toEqual({ clients: base, extraSecrets: {} })
  })
  test('extension receives fresh private secrets, retaining base values', () => {
    const first = fixtureClients(base, options), second = fixtureClients(base, options)
    expect(first.clients[0]).toEqual(base[0])
    expect(first.extraSecrets.extra).toMatch(/^[a-f0-9]{64}$/)
    expect(first.extraSecrets.extra).not.toEqual(second.extraSecrets.extra)
    expect(first.clients[1].secret).toEqual(first.extraSecrets.extra)
    expect(Object.isFrozen(first.extraSecrets)).toBe(true)
    expect(base).toEqual([{ clientId: 'terrace-web', secret: 'synthetic-base-only' }])
  })
  test('bounds extension names and factories before invoking callbacks', () => {
    let calls = 0
    const callback = () => { calls++; return factory({extra: 'not-used'}) }
    for (const names of [Array(9).fill('extra'), ['extra', 'extra'], ['UPPER'], ['../path'], ['a'.repeat(33)]]) rejects({ ...options, extraSecretNames: names })
    rejects({ ...options, extraClientFactories: Array(9).fill(callback) })
    expect(calls).toBe(0)
  })
  test('requires code/PKCE with scoped roles and no alternate grants', () => {
    for (const [name, value] of Object.entries({ protocol: 'saml', enabled: false, publicClient: true, fullScopeAllowed: true, standardFlowEnabled: false, directAccessGrantsEnabled: true, implicitFlowEnabled: true, serviceAccountsEnabled: true, attributes: {} })) {
      rejects({ ...options, extraClientFactories: [(secrets: Record<string,string>) => ({ ...factory(secrets), [name]: value })] })
    }
  })
  test('requires a generated extension secret and unique client identities', () => {
    rejects({ ...options, extraClientFactories: [(secrets: Record<string,string>) => ({ ...factory(secrets), secret: 'synthetic-unmanaged' })] })
    rejects({ ...options, extraClientFactories: [(secrets: Record<string,string>) => ({ ...factory(secrets), clientId: 'terrace-web' })] })
    rejects({ ...options, extraClientFactories: [(secrets: Record<string,string>) => ({ ...factory(secrets), clientId: null })] })
  })
  test('requires distinct extension secrets and bounded exact HTTPS callbacks', () => {
    rejects({ ...options, extraClientFactories: [factory, (secrets: Record<string,string>) => ({ ...factory(secrets), clientId: 'terrace-another' })] })
    for (const value of [[], ['http://extra.terrace.invalid/auth/callback'], ['https://extra.terrace.invalid/*'], ['https://user:synthetic@extra.terrace.invalid/auth/callback'], Array(5).fill('https://extra.terrace.invalid/auth/callback')]) {
      rejects({ ...options, extraClientFactories: [(secrets: Record<string,string>) => ({ ...factory(secrets), redirectUris: value })] })
    }
    for (const value of [[], ['https://extra.terrace.invalid/'], ['https://extra.terrace.invalid/path'], ['*']]) {
      rejects({ ...options, extraClientFactories: [(secrets: Record<string,string>) => ({ ...factory(secrets), webOrigins: value })] })
    }
  })
  test('suppresses factory exceptions carrying sensitive values', () => {
    rejects({ ...options, extraClientFactories: [() => { throw Error('synthetic-private-diagnostic') }] })
  })
  test('does not insert accounts or change the original realm contract', () => {
    const result = fixtureClients(base, options)
    expect(Object.keys(result).sort()).toEqual(['clients', 'extraSecrets'])
    expect(result.clients).toHaveLength(2)
    expect(base).toHaveLength(1)
  })
})
