import { chmodSync, chownSync, lstatSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { createHash } from 'node:crypto'

const rejected = () => new Error('Documenso runtime inputs rejected.')
const secretKeys = ['authSecret', 'encryptionKey', 'encryptionSecondaryKey', 'oidcClientSecret', 'databasePassword'] as const
type Account = { email: string; name: string; role: 'USER' | 'ADMIN'; password?: string }
const object = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw rejected()
  return value as Record<string, unknown>
}
function account(value: unknown, recovery = false): Account {
  const source = object(value)
  const keys = recovery ? ['email', 'name', 'password'] : ['email', 'name', 'role']
  if (Object.keys(source).some(key => !keys.includes(key)) || typeof source.email !== 'string' || source.email.length > 254 || !/^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}$/i.test(source.email) || typeof source.name !== 'string' || source.name.length < 3 || source.name.length > 80 || /[\u0000-\u001f]/.test(source.name)) throw rejected()
  if (recovery && (typeof source.password !== 'string' || !/^[A-Za-z0-9_+/=-]{32,72}$/.test(source.password))) throw rejected()
  if (!recovery && source.role !== 'USER' && source.role !== 'ADMIN') throw rejected()
  return { email: source.email.toLowerCase(), name: source.name, role: recovery ? 'ADMIN' : source.role as Account['role'], ...(recovery ? { password: source.password as string } : {}) }
}
export function runtimeInputs(value: unknown) {
  try {
    const source = object(value)
    if (Object.keys(source).length !== 7 || ![...secretKeys, 'recoveryUser', 'approvedUsers'].every(key => Object.hasOwn(source, key))) throw rejected()
    const secrets = Object.fromEntries(secretKeys.map(key => {
      const value = source[key]
      if (typeof value !== 'string' || !/^[A-Za-z0-9_+/=-]{32,256}$/.test(value)) throw rejected()
      return [key, value]
    })) as Record<typeof secretKeys[number], string>
    if (new Set(Object.values(secrets)).size !== secretKeys.length || !Array.isArray(source.approvedUsers) || source.approvedUsers.length > 32) throw rejected()
    const accounts = [account(source.recoveryUser, true), ...source.approvedUsers.map(value => account(value))]
    if (new Set(accounts.map(value => value.email)).size !== accounts.length) throw rejected()
    return { secrets, accounts }
  } catch { throw rejected() }
}
export function databaseUrl(password: string, limit: 1 | 6) {
  return `postgresql://terrace_documenso:${encodeURIComponent(password)}@localhost:5432/terrace_documenso?host=/run/postgresql&connection_limit=${limit}&pool_timeout=10&connect_timeout=5`
}
export const inputFingerprint = (value: unknown) => createHash('sha256').update(JSON.stringify(runtimeInputs(value))).digest('hex')

if (import.meta.main) {
  try {
    const checking = process.argv[2] === '--check'
    if (checking ? process.argv.length !== 7 : process.argv.length !== 4) throw rejected()
    const source = process.argv[checking ? 3 : 2]!, output = process.argv[checking ? 4 : 3]!, stat = lstatSync(source)
    if (!stat.isFile() || stat.uid !== 0 || (stat.mode & 0o777) !== 0o400 || stat.size > 65536) throw rejected()
    const input = JSON.parse(readFileSync(source, 'utf8'))
    const { secrets, accounts } = runtimeInputs(input), fingerprint = inputFingerprint(input)
    if (checking) {
      if (readFileSync(join(output, 'inputs.sha256'), 'utf8').trim() !== fingerprint || readFileSync(process.argv[5]!, 'utf8').trim() !== `${process.argv[6]}\n${fingerprint}`) throw rejected()
      process.exit(0)
    }
    mkdirSync(output, { recursive: true, mode: 0o700 }); chmodSync(output, 0o700)
    const credentials = join(output, 'credentials')
    mkdirSync(credentials, { mode: 0o500, recursive: true }); chownSync(credentials, 1001, 1001); chmodSync(credentials, 0o500)
    writeFileSync(join(credentials, 'accounts.json'), JSON.stringify(accounts), { mode: 0o400 }); chownSync(join(credentials, 'accounts.json'), 1001, 1001)
    const common = { NEXTAUTH_SECRET: secrets.authSecret, NEXT_PRIVATE_ENCRYPTION_KEY: secrets.encryptionKey, NEXT_PRIVATE_ENCRYPTION_SECONDARY_KEY: secrets.encryptionSecondaryKey, NEXT_PRIVATE_OIDC_CLIENT_SECRET: secrets.oidcClientSecret }
    for (const [name, limit] of [['runtime', 6], ['migration', 1]] as const) {
      const url = databaseUrl(secrets.databasePassword, limit)
      writeFileSync(join(output, `${name}.env`), Object.entries({ ...common, NEXT_PRIVATE_DATABASE_URL: url, NEXT_PRIVATE_DIRECT_DATABASE_URL: url }).map(([key, value]) => `${key}=${value}`).join('\n') + '\n', { mode: 0o600 })
    }
    // psql receives the password through a private file/stdin, never argv.
    writeFileSync(join(output, 'database.sql'), `ALTER ROLE terrace_documenso WITH LOGIN CONNECTION LIMIT 6 PASSWORD '${secrets.databasePassword}';\n`, { mode: 0o600 })
    writeFileSync(join(output, 'inputs.sha256'), fingerprint + '\n', { mode: 0o600 })
  } catch {
    console.error('Documenso runtime input preparation failed; private diagnostics suppressed.')
    process.exitCode = 1
  }
}
