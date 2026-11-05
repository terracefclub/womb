// Runs only inside a disposable guest. Imported contract files are hash-pinned
// copies of the accepted TFC source, never a checkout containing private state.
import { mkdirSync, writeFileSync } from 'node:fs'
import { randomBytes } from 'node:crypto'
import { productionRealm } from './identity/tools/production-realm'
import { syntheticIdentityRoles } from './identity/tools/realm-config'
import { fixtureClients, type FixtureOptions } from './fixture-clients'

export function createIdentityFixture(options: FixtureOptions = {}) {
  const directory = '/var/lib/terrace-gateway-test'
  const inputs = '/var/lib/terrace-website-inputs'
  const secret = () => randomBytes(32).toString('hex')
  mkdirSync(directory, { recursive: true, mode: 0o700 })
  mkdirSync(inputs, { recursive: true, mode: 0o700 })
  const write = (path: string, value: string) => writeFileSync(path, value, { mode: 0o400, flag: 'wx' })
  const credentials = { webSecret: secret(), introspectionSecret: secret(), cmsSecret: secret(), editorialSecret: secret() }
  const artifact = productionRealm('https://web.terrace.invalid', 'https://cms.terrace.invalid', credentials)
  const { clients, extraSecrets } = fixtureClients(artifact.clients, options)
  write(`${directory}/terrace-realm.json`, JSON.stringify({ ...artifact, clients }))
  const adminPassword = secret()
  const users = syntheticIdentityRoles.map(user => ({ ...user, email: `${user.username}@terrace.invalid`, password: secret() }))
  write(`${directory}/credentials.json`, JSON.stringify({ adminPassword, users }))
  // This temporary master bootstrap is test-only and is deleted after creating
  // the synthetic users through the private loopback admin API. No public admin route.
  write(`${directory}/keycloak.env`, `KC_BOOTSTRAP_ADMIN_USERNAME=gateway-fixture-bootstrap\nKC_BOOTSTRAP_ADMIN_PASSWORD=${adminPassword}\n`)
  write(`${directory}/db-password`, secret())
  const issuer = 'https://identity.terrace.invalid/realms/terrace'
  write(`${inputs}/cms.env`, `PAYLOAD_SECRET=${secret()}
CMS_PUBLIC_DEPLOYMENT=1
CMS_ORIGIN=https://cms.terrace.invalid
CMS_MEDIA_DIR=/var/lib/terrace-cms-media
CMS_AUTH_ISSUER=${issuer}
CMS_AUTH_CLIENT_ID=terrace-cms
CMS_AUTH_AUDIENCE=terrace-editorial
CMS_AUTH_CLIENT_SECRET=${credentials.cmsSecret}
CMS_AUTH_INTROSPECTION_CLIENT_ID=terrace-editorial
CMS_AUTH_INTROSPECTION_SECRET=${credentials.editorialSecret}
MEMBER_AUTH_ISSUER=${issuer}
MEMBER_AUTH_CLIENT_ID=terrace-web
MEMBER_AUTH_AUDIENCE=terrace-community
MEMBER_AUTH_INTROSPECTION_CLIENT_ID=terrace-community
MEMBER_AUTH_INTROSPECTION_SECRET=${credentials.introspectionSecret}
`)
  write(`${inputs}/web.env`, `CMS_ORIGIN=https://cms.terrace.invalid
CONTENT_SOURCE=cms
ORIGIN=https://web.terrace.invalid
MEMBER_AUTH_ORIGIN=https://web.terrace.invalid
MEMBER_AUTH_ISSUER=${issuer}
MEMBER_AUTH_CLIENT_ID=terrace-web
MEMBER_AUTH_CLIENT_SECRET=${credentials.webSecret}
`)
  return { extraSecrets, users, issuer, directory, inputs }
}

if (import.meta.main) {
  try { createIdentityFixture() }
  catch {
    console.error('Synthetic identity fixture preparation failed; private diagnostics suppressed.')
    process.exitCode = 1
  }
}
