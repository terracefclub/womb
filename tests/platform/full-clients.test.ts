import { expect, test } from 'bun:test'
import { applicationClients } from '../../platform/full/clients'
import { fixtureClients } from '../../platform/website-gateway/fixture-clients'

test('three applications compose reviewed isolated clients without altering existing realm clients', () => {
  const existing = { clientId: 'existing-reviewed-client' }
  const { clients, extraSecrets } = fixtureClients([existing], applicationClients)
  expect(clients[0]).toBe(existing)
  expect(clients.slice(1).map(client => client.clientId)).toEqual(['terrace-documenso', 'terrace-windmill', 'terrace-vaultwarden'])
  expect(new Set(clients.slice(1).map(client => client.secret)).size).toBe(3)
  expect(Object.keys(extraSecrets).sort()).toEqual(['documenso', 'vaultwarden', 'windmill'])
  expect(clients.slice(1).map(client => client.secret)).toEqual([extraSecrets.documenso, extraSecrets.windmill, extraSecrets.vaultwarden])
  expect((clients[3]!.attributes as Record<string, string>)['access.token.lifespan']).toBe('600')
})
