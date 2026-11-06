import { expect, test } from 'bun:test'
import { databaseUrl, inputFingerprint, runtimeInputs } from '../../platform/documenso/inputs'
import { documensoClient } from '../../platform/documenso/client'
import { writeDocumensoInputs } from '../../platform/documenso/write-inputs'
import { mkdtempSync, readFileSync, readdirSync, rmSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

function fixture() {
  return {
    authSecret: 'a'.repeat(48), encryptionKey: 'b'.repeat(48), encryptionSecondaryKey: 'c'.repeat(48),
    oidcClientSecret: 'd'.repeat(48), databasePassword: 'e'.repeat(48),
    recoveryUser: { name: 'Recovery Fixture', email: 'recovery@terrace.invalid', password: 'f'.repeat(48) },
    approvedUsers: [{ name: 'Approved Fixture', email: 'approved@terrace.invalid', role: 'USER' }],
  }
}
test('shared fixture writer creates private app inputs without generating another realm or exposing server secrets to browser', () => {
  const directory = mkdtempSync(join(tmpdir(), 'documenso-shared-inputs-'))
  try {
    const users = ['local-member', 'local-alumni', 'local-editorial-admin'].map(username => ({
      username, email: `${username}@terrace.invalid`, password: 'synthetic-browser-password',
    }))
    const shared = { directory, users, extraSecrets: { documenso: 'd'.repeat(48) } } as Parameters<typeof writeDocumensoInputs>[0]
    const inputs = join(directory, 'app')
    writeDocumensoInputs(shared, { directory: inputs, signing: false })
    expect(readdirSync(directory).sort()).toEqual(['app', 'documenso-browser.json'])
    expect(statSync(inputs).mode & 0o777).toBe(0o700)
    expect(readdirSync(inputs)).toEqual(['secrets.json'])
    for (const name of readdirSync(inputs)) expect(statSync(join(inputs, name)).mode & 0o777).toBe(0o400)
    const browserPath = join(directory, 'documenso-browser.json')
    const browser = JSON.parse(readFileSync(browserPath, 'utf8'))
    expect(statSync(browserPath).mode & 0o777).toBe(0o400)
    expect(browser).toEqual({ users })
    const server = JSON.parse(readFileSync(join(inputs, 'secrets.json'), 'utf8'))
    expect(runtimeInputs(server).accounts).toHaveLength(4)
    expect(server.oidcClientSecret).toBe(shared.extraSecrets.documenso)
    for (const name of ['authSecret', 'encryptionKey', 'encryptionSecondaryKey', 'databasePassword', 'oidcClientSecret']) {
      expect(JSON.stringify(browser)).not.toContain(server[name])
    }
    expect(() => writeDocumensoInputs(shared, { directory: inputs, signing: false })).toThrow()
  } finally { rmSync(directory, { recursive: true, force: true }) }
}, 15000)
test('runtime inputs keep explicit approved accounts separate from the recovery administrator', () => {
  const value = runtimeInputs(fixture())
  expect(value.accounts).toHaveLength(2)
  expect(value.accounts[0]!.role).toBe('ADMIN')
  expect(value.accounts[1]!.role).toBe('USER')
  expect(value.accounts[1]!.password).toBeUndefined()
  expect(Object.keys(value.secrets)).toHaveLength(5)
})
test('input readiness binds every validated secret and approval change but ignores JSON key order', () => {
  const value = fixture(), original = inputFingerprint(value)
  expect(original).toMatch(/^[a-f0-9]{64}$/)
  expect(inputFingerprint(Object.fromEntries(Object.entries(value).reverse()))).toBe(original)
  expect(inputFingerprint({ ...value, encryptionKey: 'g'.repeat(48) })).not.toBe(original)
  expect(inputFingerprint({ ...value, approvedUsers: [] })).not.toBe(original)
  expect(inputFingerprint({ ...value, recoveryUser: { ...value.recoveryUser, password: 'h'.repeat(48) } })).not.toBe(original)
})
test('recovery passwords respect the native bcrypt/login byte ceiling', () => {
  const value = fixture()
  expect(runtimeInputs({ ...value, recoveryUser: { ...value.recoveryUser, password: 'x'.repeat(72) } }).accounts[0]!.password).toHaveLength(72)
  expect(() => runtimeInputs({ ...value, recoveryUser: { ...value.recoveryUser, password: 'x'.repeat(73) } })).toThrow('Documenso runtime inputs rejected.')
})
test('invalid secrets, duplicate approval identities and unknown fields fail with static errors', () => {
  const value = fixture(), marker = 'PRIVATE_DOCUMENSO_INPUT_MARKER'
  for (const bad of [null, [], { ...value, extra: marker }, { ...value, authSecret: marker }, { ...value, databasePassword: 'x'.repeat(32) + "'" }, { ...value, encryptionSecondaryKey: value.encryptionKey }, { ...value, approvedUsers: [value.approvedUsers[0], value.approvedUsers[0]] }, { ...value, approvedUsers: [{ ...value.approvedUsers[0], email: value.recoveryUser.email }] }, { ...value, approvedUsers: [{ ...value.approvedUsers[0], role: marker }] }, { ...value, approvedUsers: [{ ...value.approvedUsers[0], password: marker }] }]) {
    expect(() => runtimeInputs(bad)).toThrow('Documenso runtime inputs rejected.')
    try { runtimeInputs(bad) } catch (error) { expect((error as Error).message).not.toContain(marker) }
  }
})
test('runtime and offline migration URLs use the private native socket and explicit bounded pools', () => {
  const password = 'synthetic+password/value=' + 'x'.repeat(32)
  for (const limit of [1, 6] as const) {
    const parsed = new URL(databaseUrl(password, limit))
    expect(parsed.username).toBe('terrace_documenso')
    expect(decodeURIComponent(parsed.password)).toBe(password)
    expect(parsed.hostname).toBe('localhost')
    expect(parsed.pathname).toBe('/terrace_documenso')
    expect(parsed.searchParams.get('host')).toBe('/run/postgresql')
    expect(parsed.searchParams.get('connection_limit')).toBe(String(limit))
    expect(parsed.searchParams.get('pool_timeout')).toBe('10')
  }
})
test('user-free initial realm client enables only confidential PKCE authorization-code access', () => {
  const client = documensoClient('https://documents.terrace.invalid', 'x'.repeat(48))
  expect(client).toMatchObject({ clientId: 'terrace-documenso', publicClient: false, standardFlowEnabled: true, directAccessGrantsEnabled: false, implicitFlowEnabled: false, serviceAccountsEnabled: false, fullScopeAllowed: false })
  expect(client.redirectUris).toEqual(['https://documents.terrace.invalid/api/auth/callback/oidc'])
  expect(client.webOrigins).toEqual(['https://documents.terrace.invalid'])
  expect(client.defaultClientScopes).toEqual(['basic', 'profile', 'email'])
  expect(client.attributes['pkce.code.challenge.method']).toBe('S256')
})
test('identity contract rejects unsafe origins and never quotes rejected values', () => {
  const marker = 'PRIVATE_DOCUMENSO_ORIGIN_MARKER'
  for (const origin of ['http://documents.invalid', 'https://localhost', 'https://127.0.0.1', 'https://documents.invalid/', 'https://documents.invalid:443', `https://user:${marker}@documents.invalid`, `https://documents.invalid?${marker}`, 'https://*.invalid', 'https://bad..invalid', marker]) {
    expect(() => documensoClient(origin, 'x'.repeat(48))).toThrow('Documenso identity client input rejected.')
  }
})
