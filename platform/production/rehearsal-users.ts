// Rehearsal only: the synthetic accounts the shared protocol enrols, with fresh
// random passwords, written to stdout for `terrace-secrets set` (never a file).
import { randomBytes } from 'node:crypto'
import { syntheticIdentityRoles } from './identity/tools/realm-config'

process.stdout.write(JSON.stringify(syntheticIdentityRoles.map(user => ({
  ...user, email: `${user.username}@terrace.invalid`, password: randomBytes(32).toString('hex'),
}))))
