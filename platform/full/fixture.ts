// Disposable guests only. One user-free realm precedes each app's private input.
import { createIdentityFixture } from '../website-gateway/fixture'
import { applicationClients } from './clients'
import { writeDocumensoInputs } from '../documenso/write-inputs'
import { writeWindmillInputs } from '../windmill-functional/write-inputs'
import { writeVaultwardenInputs } from '../vaultwarden/functional-fixture'

if (import.meta.main) {
  try {
    const fixture = createIdentityFixture(applicationClients)
    writeDocumensoInputs(fixture)
    writeWindmillInputs(fixture)
    writeVaultwardenInputs(fixture)
  } catch {
    console.error('Disposable multi-service fixture preparation failed; private diagnostics suppressed.')
    process.exitCode = 1
  }
}
