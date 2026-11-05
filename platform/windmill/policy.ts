// Pure initial realm/client and approved-user runtime policy; no I/O.
const failed = () => new Error('Windmill bootstrap rejected.')
export const validSecret = (value: unknown): value is string => typeof value === 'string' && Buffer.byteLength(value) >= 32 && Buffer.byteLength(value) <= 4096 && value === value.trim() && !/[\p{Cc}\p{Cf}]/u.test(value)
export const validOrigin = (value: unknown): value is string => {
  if (typeof value !== 'string') return false
  try {
    const u = new URL(value)
    const labels = u.hostname.split('.')
    return value === u.origin && u.protocol === 'https:' && u.hostname.length <= 253 && labels.length >= 2 && labels.every(label => label.length <= 63 && /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/.test(label)) && /[a-z]/.test(labels.at(-1)!) && !u.hostname.endsWith('.localhost') && !u.username && !u.password
  } catch { return false }
}
export type ApprovedUser = { email: string; name: string; role: 'admin' | 'operator' }
export type Input = { adminPassword: string; origin: string; issuer: string; clientId: string; clientSecret: string; approvedUsers: ApprovedUser[] }
// Match the pinned KeycloakSetting.svelte producer, including its realm URL.
export function keycloakProvider(input: Input) {
  return { id: input.clientId, secret: input.clientSecret, org: input.issuer, display_name: 'Keycloak', pkce: true, login_config: {
    auth_url: input.issuer + '/protocol/openid-connect/auth', token_url: input.issuer + '/protocol/openid-connect/token',
    userinfo_url: input.issuer + '/protocol/openid-connect/userinfo', scopes: ['openid', 'profile', 'email', 'offline_access'],
  } }
}
// Failure-only shape facts. No provider names, display names or response values.
export function providerReadbackSummary(input: unknown): string[] {
  const oauth = input && typeof input === 'object' && !Array.isArray(input) ? (input as Record<string, unknown>).oauth : undefined
  if (!Array.isArray(oauth)) return ['Windmill provider shape: oauth is not an array.']
  const objects = oauth.filter(value => value && typeof value === 'object' && !Array.isArray(value) && value.type === 'keycloak')
  const kinds = objects.map(value => value.display_name === undefined ? 'absent' : value.display_name === null ? 'null' : typeof value.display_name === 'string' ? 'string' : 'other')
  return [`Windmill provider oauth count: ${Math.min(oauth.length, 1001)}`,
    `Windmill provider Keycloak object count: ${Math.min(objects.length, 1001)}`,
    ...[...new Set(kinds)].map(kind => `Windmill provider display-name kind: ${kind}.`)]
}
// CE OpenAPI /oauth/list_logins returns objects, not the OSS stub's strings.
export function isKeycloakProviderSet(input: unknown): boolean {
  if (!input || typeof input !== 'object' || Array.isArray(input)) return false
  const value = input as Record<string, unknown>
  const absent = (field: unknown) => field === undefined || field === null || field === ''
  if (Object.keys(value).some(key => !['oauth', 'saml', 'auto_login'].includes(key)) ||
    !Array.isArray(value.oauth) || value.oauth.length !== 1 ||
    !absent(value.saml) ||
    !(absent(value.auto_login) || value.auto_login === 'keycloak')) return false
  const provider = value.oauth[0]
  return provider !== null && typeof provider === 'object' && !Array.isArray(provider) &&
    Object.keys(provider).every(key => ['type', 'display_name'].includes(key)) && provider.type === 'keycloak' &&
    (provider.display_name === undefined || (typeof provider.display_name === 'string' &&
      provider.display_name.length <= 100 && !/[\p{Cc}\p{Cf}]/u.test(provider.display_name)))
}
export function validateInput(input: unknown): Input {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw failed()
  const v = input as Input
  if (Object.keys(v).sort().join(',') !== 'adminPassword,approvedUsers,clientId,clientSecret,issuer,origin' ||
    !validSecret(v.adminPassword) || !validSecret(v.clientSecret) || v.adminPassword === v.clientSecret || !validOrigin(v.origin) ||
    typeof v.issuer !== 'string' || !v.issuer.endsWith('/realms/terrace') || !validOrigin(v.issuer.slice(0, -'/realms/terrace'.length)) ||
    v.clientId !== 'terrace-windmill' || !Array.isArray(v.approvedUsers) || v.approvedUsers.length < 1 || v.approvedUsers.length > 10) throw failed()
  const seen = new Set<string>()
  for (const user of v.approvedUsers) {
    if (!user || Object.keys(user).sort().join(',') !== 'email,name,role' || typeof user.email !== 'string' || user.email.length > 254 ||
      !/^[a-z0-9._+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+$/.test(user.email) || user.email === 'admin@windmill.dev' || seen.has(user.email) ||
      typeof user.name !== 'string' || !user.name.trim() || user.name !== user.name.trim() || user.name.length > 100 || /[\p{Cc}\p{Cf}]/u.test(user.name) ||
      !['admin', 'operator'].includes(user.role)) throw failed()
    seen.add(user.email)
  }
  if (!v.approvedUsers.some(user => user.role === 'admin')) throw failed()
  return v
}

// Initial user-free realm input only. Existing realm imports are not reconciled.
export function windmillClient(origin: string, clientSecret: string) {
  if (!validOrigin(origin) || !validSecret(clientSecret)) throw failed()
  return {
    clientId: 'terrace-windmill', name: 'Terrace Windmill', enabled: true,
    protocol: 'openid-connect', publicClient: false, fullScopeAllowed: false, secret: clientSecret,
    standardFlowEnabled: true, directAccessGrantsEnabled: false,
    implicitFlowEnabled: false, serviceAccountsEnabled: false,
    redirectUris: [origin + '/user/login_callback/keycloak'], webOrigins: [origin],
    defaultClientScopes: ['profile', 'email'], optionalClientScopes: ['offline_access'],
    attributes: {'pkce.code.challenge.method': 'S256'},
  }
}
