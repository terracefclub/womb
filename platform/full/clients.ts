// Reuse reviewed component contracts; no parallel client or realm definitions.
import type { FixtureOptions } from '../website-gateway/fixture-clients'
import { documensoClient } from '../documenso/client'
import { windmillClient } from '../windmill/policy'
import { vaultwardenClient } from '../vaultwarden/policy'

export const applicationClients: FixtureOptions = {
  extraSecretNames: ['documenso', 'windmill', 'vaultwarden'],
  extraClientFactories: [
    secrets => documensoClient('https://documents.terrace.invalid', secrets.documenso!),
    secrets => windmillClient('https://windmill.terrace.invalid', secrets.windmill!),
    secrets => vaultwardenClient('https://vault.terrace.invalid', secrets.vaultwarden!),
  ],
}
