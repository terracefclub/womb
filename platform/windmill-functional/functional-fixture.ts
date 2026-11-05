// Disposable guests only; generated credentials never enter the store or output.
import { createIdentityFixture } from './fixture'
import { windmillClient } from './policy'
import { writeWindmillInputs } from './write-inputs'

if (import.meta.main) {
  try {
    const fixture = createIdentityFixture({extraSecretNames:['windmill'],
      extraClientFactories:[secrets => windmillClient('https://windmill.terrace.invalid',secrets.windmill!)]})
    writeWindmillInputs(fixture)
  } catch {
    console.error('Windmill functional fixture preparation failed; private diagnostics suppressed.')
    process.exitCode=1
  }
}
