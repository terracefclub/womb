// Runtime-only secrets and native administrative reconciliation. No network on import.
import { constants, closeSync, fstatSync, lstatSync, mkdirSync, openSync, readSync, rmSync, writeFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { dirname } from 'node:path'
import { validateInput, type VaultwardenInput } from './policy'

const rejected = () => new Error('Vaultwarden private runtime operation rejected.')
export const fingerprint = (bytes: Uint8Array) => createHash('sha256').update(bytes).digest('hex')
export function protectedBytes(path: string, maximum = 65536): Buffer {
  const parent = lstatSync(dirname(path))
  if (!parent.isDirectory() || parent.isSymbolicLink() || parent.uid !== 0 || (parent.mode & 0o777) !== 0o700) throw rejected()
  const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
  try {
    const st = fstatSync(fd)
    if (!st.isFile() || st.uid !== 0 || (st.mode & 0o777) !== 0o400 || st.size < 1 || st.size > maximum) throw rejected()
    const buffer = Buffer.alloc(maximum + 1)
    let length = 0
    while (length <= maximum) {
      const count = readSync(fd, buffer, length, buffer.length - length, null)
      if (!count) break
      length += count
    }
    if (length !== st.size || length > maximum) throw rejected()
    return buffer.subarray(0, length)
  } finally { closeSync(fd) }
}
export function parseInput(bytes: Uint8Array, origin: string, issuer: string) {
  const input = validateInput(JSON.parse(Buffer.from(bytes).toString('utf8')))
  if (input.origin !== origin || input.issuer !== issuer) throw rejected()
  return input
}
export function runtimeEnvironment(input: VaultwardenInput, adminHash: string) {
  if (!adminHash.startsWith('$argon2id$') || /[\r\n\0]/.test(adminHash)) throw rejected()
  return Object.entries({ ADMIN_TOKEN: adminHash, SSO_CLIENT_ID: input.clientId, SSO_CLIENT_SECRET: input.clientSecret, SSO_AUTHORITY: input.issuer })
    .map(([key, value]) => `${key}=${JSON.stringify(value)}\n`).join('')
}
export function sameInput(source: Uint8Array, snapshot: Uint8Array) {
  if (fingerprint(source) !== fingerprint(snapshot)) throw rejected()
}
type User = { id: string; email: string; userEnabled: boolean }
export function nativeUsers(value: unknown): User[] {
  if (!Array.isArray(value) || value.length > 1000) throw rejected()
  const ids = new Set<string>(), emails = new Set<string>()
  for (const u of value) {
    if (!u || typeof u !== 'object' || !/^[a-f0-9-]{36}$/.test(u.id) || typeof u.email !== 'string' || u.email !== u.email.toLowerCase() || typeof u.userEnabled !== 'boolean' || ids.has(u.id) || emails.has(u.email)) throw rejected()
    ids.add(u.id); emails.add(u.email)
  }
  return value
}
// A dedicated-instance allowlist: disabling retains vault data; never delete users.
export function approvalActions(users: User[], approvedEmails: string[]) {
  const enabled = new Set(approvedEmails), existing = new Set(users.map(u => u.email))
  return {
    invite: approvedEmails.filter(email => !existing.has(email)),
    enable: users.filter(u => enabled.has(u.email) && !u.userEnabled).map(u => u.id),
    disable: users.filter(u => !enabled.has(u.email) && u.userEnabled).map(u => u.id),
  }
}
export function validateRuntimePolicy(value: unknown) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw rejected()
  const config = value as Record<string, unknown>
  const expected = {sso_enabled:true,sso_only:true,sso_pkce:true,sso_signups_allowed:false,sso_signups_match_email:true,sso_allow_unknown_email_verification:false,sso_debug_tokens:false,signups_allowed:false,signups_domains_whitelist:'',invitations_allowed:false,use_sendmail:false,smtp_host:null,push_enabled:false,disable_icon_download:true,disable_admin_token:false,database_max_conns:6,sso_audience_trusted:null,sso_auth_only_not_session:false}
  if (Object.entries(expected).some(([key,value]) => config[key] !== value)) throw rejected()
}
async function privateAdmin(input: VaultwardenInput, port: number) {
  const deadline = performance.now() + 90000
  let cookie = ''
  const request = async (path: string, method = 'GET', body?: string, form = false) => {
    const remaining = Math.min(5000, deadline - performance.now())
    if (remaining <= 0) throw rejected()
    const response = await fetch(`http://127.0.0.1:${port}${path}`, {
      method, redirect: 'manual', signal: AbortSignal.timeout(Math.max(1, Math.floor(remaining))),
      headers: { Accept:'application/json', 'Content-Type': form ? 'application/x-www-form-urlencoded' : 'application/json', ...(cookie ? {Cookie: cookie} : {}) }, body,
    })
    let size = 0; const chunks: Uint8Array[] = []
    if (response.body) for await (const chunk of response.body) {
      size += chunk.length
      if (size > 1024 * 1024) throw rejected()
      chunks.push(chunk)
    }
    if (response.status < 200 || response.status >= 300) throw rejected()
    return {response, text: Buffer.concat(chunks).toString('utf8')}
  }
  const login = await request('/admin/', 'POST', new URLSearchParams({token:input.adminToken}).toString(), true)
  const match = login.response.headers.get('set-cookie')?.match(/(?:^|,\s*)VW_ADMIN=([^;\r\n,]+)/)
  if (!match) throw rejected()
  cookie = `VW_ADMIN=${match[1]}`
  validateRuntimePolicy(JSON.parse((await request('/admin/diagnostics/config')).text))
  return request
}
async function inspect(input: VaultwardenInput, port: number) {
  const request = await privateAdmin(input, port)
  const remaining = approvalActions(nativeUsers(JSON.parse((await request('/admin/users')).text)), input.approvedEmails)
  if (remaining.invite.length || remaining.enable.length || remaining.disable.length) throw rejected()
}
async function reconcile(input: VaultwardenInput, port: number) {
  const request = await privateAdmin(input, port)
  const list = async () => nativeUsers(JSON.parse((await request('/admin/users')).text))
  const actions = approvalActions(await list(), input.approvedEmails)
  for (const id of actions.disable) await request(`/admin/users/${id}/disable`, 'POST')
  for (const email of actions.invite) await request('/admin/invite', 'POST', JSON.stringify({email}))
  for (const id of actions.enable) await request(`/admin/users/${id}/enable`, 'POST')
  const remaining = approvalActions(await list(), input.approvedEmails)
  if (remaining.invite.length || remaining.enable.length || remaining.disable.length) throw rejected()
}

if (import.meta.main) {
  const [command, source, origin, issuer, portText] = process.argv.slice(2)
  const runtime = '/run/terrace-vaultwarden-inputs', ready = '/run/terrace-vaultwarden-state/ready'
  try {
    const adminOperation = command === 'bootstrap' || command === 'inspect'
    if (!source || !origin || !issuer || !['prepare','check','bootstrap','inspect'].includes(command!) || process.argv.length !== (adminOperation ? 7 : 6)) throw rejected()
    const bytes = protectedBytes(source), input = parseInput(bytes, origin, issuer)
    if (command === 'prepare') {
      mkdirSync(runtime, {recursive:true, mode:0o700})
      const directory = lstatSync(runtime)
      if (!directory.isDirectory() || directory.isSymbolicLink() || directory.uid !== 0 || (directory.mode & 0o777) !== 0o700) throw rejected()
      const hash = await Bun.password.hash(input.adminToken, {algorithm:'argon2id', memoryCost:65536, timeCost:3})
      for (const [name, data] of [['input.json',bytes],['runtime.env',runtimeEnvironment(input, hash)]] as const) {
        rmSync(`${runtime}/${name}`, {force:true});writeFileSync(`${runtime}/${name}`,data,{mode:0o400,flag:'wx'})
      }
    } else {
      sameInput(bytes, protectedBytes(`${runtime}/input.json`))
      if (adminOperation) {
        const port = Number(portText)
        if (!Number.isInteger(port) || port < 1024 || port > 65535 || !ready) throw rejected()
        if (command === 'inspect') await inspect(input, port)
        else await reconcile(input, port)
        sameInput(protectedBytes(source), protectedBytes(`${runtime}/input.json`))
        if (command === 'inspect') console.log('Native Vaultwarden private admin readback passed.')
        else writeFileSync(ready, fingerprint(bytes)+'\n', {mode:0o600,flag:'wx'})
      }
    }
  } catch {
    if (command === 'bootstrap') { try { rmSync(ready,{force:true}) } catch {} }
    console.error(command === 'inspect' ? 'Vaultwarden private inspection rejected; diagnostics suppressed.' : 'Vaultwarden private runtime operation rejected; ingress remains closed.')
    process.exitCode = 1
  }
}
