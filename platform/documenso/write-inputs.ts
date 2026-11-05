// Disposable input writer shared by standalone and combined VM rehearsals.
import { chmodSync, mkdirSync, writeFileSync } from 'node:fs'
import { randomBytes } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import type { createIdentityFixture } from './fixture'
import { runtimeInputs } from './inputs'

export function writeDocumensoInputs(
  fixture: ReturnType<typeof createIdentityFixture>,
  { directory = '/var/lib/terrace-documenso-inputs', signing = true }: { directory?: string; signing?: boolean } = {},
) {
  const secret = () => randomBytes(40).toString('base64url')
  const recoveryUser = { email: 'recovery@terrace.invalid', name: 'Synthetic Recovery', password: secret() }
  if (fixture.users.some(user => user.email === recoveryUser.email)) throw Error()
  const approvals = [
    { username: 'local-member', role: 'USER' },
    { username: 'local-alumni', role: 'USER' },
    { username: 'local-editorial-admin', role: 'ADMIN' },
  ] as const
  const approvedUsers = approvals.map(({ username, role }) => {
    const user = fixture.users.find(user => user.username === username)
    if (!user) throw Error()
    return { email: user.email, name: `Synthetic ${username}`, role }
  })
  const inputs = {
    authSecret: secret(), encryptionKey: secret(), encryptionSecondaryKey: secret(),
    databasePassword: secret(), oidcClientSecret: fixture.extraSecrets.documenso,
    recoveryUser, approvedUsers,
  }
  runtimeInputs(inputs)
  mkdirSync(directory, { recursive: true, mode: 0o700 })
  writeFileSync(`${directory}/secrets.json`, JSON.stringify(inputs), { mode: 0o400, flag: 'wx' })
  if (signing) {
    writeFileSync(`${directory}/signing-passphrase`, secret(), { mode: 0o400, flag: 'wx' })
    for (const arguments_ of [
      ['req', '-x509', '-newkey', 'rsa:2048', '-sha256', '-days', '365', '-nodes',
        '-keyout', `${directory}/signing.key`, '-out', `${directory}/signing.crt`,
        '-subj', '/CN=Synthetic Rehearsal Signing Only', '-addext', 'basicConstraints=critical,CA:FALSE',
        '-addext', 'keyUsage=critical,digitalSignature,nonRepudiation'],
      ['pkcs12', '-export', '-inkey', `${directory}/signing.key`, '-in', `${directory}/signing.crt`,
        '-out', `${directory}/signing.p12`, '-passout', `file:${directory}/signing-passphrase`],
    ]) {
      const result = spawnSync('openssl', arguments_, { stdio: 'ignore', timeout: 20000 })
      if (result.status !== 0) throw Error()
    }
    for (const name of ['signing.key', 'signing.crt', 'signing.p12']) chmodSync(`${directory}/${name}`, 0o400)
  }
  // Browser receives no database/encryption/client/master-administrator secrets.
  writeFileSync(`${fixture.directory}/documenso-browser.json`, JSON.stringify({
    users: fixture.users.map(({ username, email, password }) => ({ username, email, password })),
  }), { mode: 0o400, flag: 'wx' })
}
