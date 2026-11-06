// Runs only inside a disposable guest. Generates one realm, then app inputs.
import { createIdentityFixture } from './fixture'
import { documensoClient } from './client'
import { writeDocumensoInputs } from './write-inputs'

try {
  const fixture = createIdentityFixture({
    extraSecretNames: ['documenso'],
    extraClientFactories: [secrets => documensoClient('https://documents.terrace.invalid', secrets.documenso!)],
  })
  writeDocumensoInputs(fixture)
} catch {
  console.error('Disposable Documenso fixture preparation failed; private diagnostics suppressed.')
  process.exitCode = 1
}
