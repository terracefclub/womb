// Pure operator approval and initial client contract. No accounts or I/O.
const rejected = () => new Error('Vaultwarden runtime input rejected.')
const secret = (value: unknown): value is string => typeof value === 'string' &&
  Buffer.byteLength(value) >= 32 && Buffer.byteLength(value) <= 4096 && value === value.trim() && !/[\p{Cc}\p{Cf}]/u.test(value)
const hostname = (value: string) => {
  const labels = value.split('.')
  return value.length <= 253 && labels.length >= 2 && /[a-z]/.test(labels.at(-1)!) &&
    labels.every(label => label.length <= 63 && /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/.test(label))
}
const email = (value: unknown): value is string => {
  if (typeof value !== 'string' || value.length > 254 || !/^[a-z0-9._+-]+@[a-z0-9.-]+$/.test(value)) return false
  const [local, domain] = value.split('@')
  return local!.length <= 64 && !local!.startsWith('.') && !local!.endsWith('.') && !local!.includes('..') && hostname(domain!)
}
const origin = (value: unknown): value is string => {
  if (typeof value !== 'string') return false
  try {
    const url = new URL(value)
    return value === url.origin && url.protocol === 'https:' && !url.username && !url.password &&
      hostname(url.hostname) && !url.hostname.endsWith('.localhost')
  } catch { return false }
}
export type VaultwardenInput = {
  origin: string; issuer: string; clientId: 'terrace-vaultwarden';
  clientSecret: string; adminToken: string; approvedEmails: string[];
}
export function validateInput(value: unknown): VaultwardenInput {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw rejected()
  const input = value as VaultwardenInput
  if (Object.keys(input).sort().join(',') !== 'adminToken,approvedEmails,clientId,clientSecret,issuer,origin' ||
      !origin(input.origin) || typeof input.issuer !== 'string' || !input.issuer.endsWith('/realms/terrace') ||
      !origin(input.issuer.slice(0, -'/realms/terrace'.length)) || input.clientId !== 'terrace-vaultwarden' ||
      !secret(input.adminToken) || !secret(input.clientSecret) || input.adminToken === input.clientSecret ||
      !Array.isArray(input.approvedEmails) || input.approvedEmails.length < 1 || input.approvedEmails.length > 100 ||
      new Set(input.approvedEmails).size !== input.approvedEmails.length ||
      input.approvedEmails.some(value => !email(value))) throw rejected()
  return input
}
// Append to the reviewed user-free production realm. Existing realms need
// deliberate reconciliation; startup import does not update them.
export function vaultwardenClient(appOrigin: string, clientSecret: string) {
  if (!origin(appOrigin) || !secret(clientSecret)) throw rejected()
  return {
    clientId: 'terrace-vaultwarden', name: 'Terrace Vaultwarden', enabled: true,
    protocol: 'openid-connect', publicClient: false, fullScopeAllowed: false, secret: clientSecret,
    standardFlowEnabled: true, directAccessGrantsEnabled: false,
    implicitFlowEnabled: false, serviceAccountsEnabled: false,
    redirectUris: [appOrigin + '/identity/connect/oidc-signin'], webOrigins: [appOrigin],
    defaultClientScopes: ['profile', 'email'], optionalClientScopes: [],
    attributes: {'pkce.code.challenge.method': 'S256'},
  }
}
