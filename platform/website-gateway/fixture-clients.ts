// Test-only composition of reviewed client factories. Never an account importer.
import { randomBytes } from 'node:crypto'

type Client = Record<string, unknown>
type ClientFactory = (extraSecrets: Readonly<Record<string, string>>) => Client
export type FixtureOptions = { extraSecretNames?: readonly string[]; extraClientFactories?: readonly ClientFactory[] }

export function fixtureClients(base: readonly Client[], options: FixtureOptions = {}) {
  const reject = () => { throw Error('Synthetic fixture extension rejected.') }
  const names = options.extraSecretNames || []
  const factories = options.extraClientFactories || []
  if (!Array.isArray(names) || !Array.isArray(factories) || names.length > 8 || factories.length > 8 ||
      new Set(names).size !== names.length || names.some(name => typeof name !== 'string' || !/^[a-z][a-z0-9-]{0,31}$/.test(name))) reject()
  const extraSecrets: Readonly<Record<string, string>> = Object.freeze(Object.fromEntries(names.map(name => [name, randomBytes(32).toString('hex')])))
  const https = (value: unknown, originOnly = false) => {
    if (typeof value !== 'string' || value.length > 2048 || value.includes('*')) return false
    try {
      const url = new URL(value)
      return url.protocol === 'https:' && !url.username && !url.password && !url.hash &&
        /^[a-z0-9.-]+$/.test(url.hostname) && (originOnly ? url.origin === value : url.href === value)
    } catch { return false }
  }
  const extras = factories.map(factory => {
    let client: Client
    try { client = factory(extraSecrets) } catch { return reject() }
    if (!client || client.protocol !== 'openid-connect' || client.enabled !== true || !Object.values(extraSecrets).includes(client.secret as string) || client.publicClient !== false || client.fullScopeAllowed !== false ||
        client.standardFlowEnabled !== true || client.directAccessGrantsEnabled !== false || client.implicitFlowEnabled !== false || client.serviceAccountsEnabled !== false ||
        (client.attributes as Record<string, unknown>)?.['pkce.code.challenge.method'] !== 'S256' ||
        !Array.isArray(client.redirectUris) || client.redirectUris.length < 1 || client.redirectUris.length > 4 || client.redirectUris.some(value => !https(value)) ||
        !Array.isArray(client.webOrigins) || client.webOrigins.length < 1 || client.webOrigins.length > 4 || client.webOrigins.some(value => !https(value, true))) reject()
    return client
  })
  if (new Set(extras.map(client => client.secret)).size !== extras.length) reject()
  const clients = [...base, ...extras]
  if (clients.some(client => typeof client.clientId !== 'string') || new Set(clients.map(client => client.clientId)).size !== clients.length) reject()
  return { clients, extraSecrets }
}
