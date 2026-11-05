// Disposable runtime only: compose the pinned producer, never a local realm export.
import { mkdirSync, writeFileSync } from 'node:fs'
import { randomBytes } from 'node:crypto'
import { productionRealm } from './identity/tools/production-realm'
import { fixtureClients } from './fixture-clients'
import { windmillClient } from './policy'

try {
  const root = '/var/lib/terrace-windmill-inputs'
  mkdirSync(root, { mode: 0o700, recursive: true })
  const secret = () => randomBytes(32).toString('hex')
  const base = productionRealm('https://web.terrace.invalid', 'https://cms.terrace.invalid', {
    webSecret: secret(), introspectionSecret: secret(), cmsSecret: secret(), editorialSecret: secret(),
  })
  const { clients, extraSecrets } = fixtureClients(base.clients, {
    extraSecretNames: ['windmill'],
    extraClientFactories: [secrets => windmillClient('https://windmill.terrace.invalid', secrets.windmill!)],
  })
  const write = (name: string, value: string) => writeFileSync(`${root}/${name}`, value, {mode: 0o400, flag: 'wx'})
  // No account bootstrap is needed to prove provider registration. This realm is
  // user-free, and no master bootstrap administrator is configured.
  write('terrace-realm.json', JSON.stringify({...base, clients}))
  write('db-password', secret())
  write('bootstrap.json', JSON.stringify({
    adminPassword: secret(), origin: 'https://windmill.terrace.invalid',
    issuer: 'https://identity.terrace.invalid/realms/terrace', clientId: 'terrace-windmill',
    clientSecret: extraSecrets.windmill,
    approvedUsers: [{email:'operator@terrace.invalid', name:'Synthetic Operator', role:'operator'},
      {email:'admin@terrace.invalid', name:'Synthetic Administrator', role:'admin'}],
  }))
} catch {
  console.error('Windmill runtime identity fixture preparation failed; private diagnostics suppressed.')
  process.exitCode = 1
}
