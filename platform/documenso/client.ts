// Compose this into the user-free initial realm. Import is create-only, not reconciliation.
import { isIP } from 'node:net'
export function documensoClient(origin: string, secret: string) {
  try {
    const parsed = new URL(origin)
    if (parsed.protocol !== 'https:' || parsed.origin !== origin || parsed.username || parsed.password || parsed.hostname === 'localhost' || isIP(parsed.hostname) || parsed.hostname.length > 253 || !parsed.hostname.split('.').every(label => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label)) || parsed.port || !/^[A-Za-z0-9_+/=-]{32,256}$/.test(secret)) throw Error()
    return {
      clientId: 'terrace-documenso', protocol: 'openid-connect', enabled: true,
      publicClient: false, clientAuthenticatorType: 'client-secret', secret,
      standardFlowEnabled: true, directAccessGrantsEnabled: false,
      implicitFlowEnabled: false, serviceAccountsEnabled: false, fullScopeAllowed: false,
      redirectUris: [`${origin}/api/auth/callback/oidc`], webOrigins: [origin],
      defaultClientScopes: ['basic', 'profile', 'email'], optionalClientScopes: [],
      attributes: { 'pkce.code.challenge.method': 'S256', 'post.logout.redirect.uris': origin },
    }
  } catch { throw new Error('Documenso identity client input rejected.') }
}
