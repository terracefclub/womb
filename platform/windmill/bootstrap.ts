// Protected runtime input; never import dotenv or print provider/account content.
import { closeSync, constants, fstatSync, lstatSync, openSync, readSync, rmSync, writeFileSync } from 'node:fs'
import { randomBytes } from 'node:crypto'
import { keycloakProvider, providerReadbackSummary, validateInput } from './policy'
import { waitForKeycloak } from './provider'

const READY = '/run/terrace-windmill-ready'
let stage = 'input policy'
const failed = () => new Error('Windmill bootstrap rejected.')

async function bootstrap(path: string, port: number, expectedOrigin: string) {
  const ready = READY;
  // Remove readiness before even parsing: failed reconciliation must close ingress.
  rmSync(ready, { force: true })
  const protectedInput = () => {
    const directory = lstatSync(path.slice(0, path.lastIndexOf('/')))
    if (!directory.isDirectory() || directory.uid !== 0 || (directory.mode & 0o777) !== 0o700) throw failed()
    const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
    try {
      const file = fstatSync(fd)
      if (!file.isFile() || file.uid !== 0 || (file.mode & 0o777) !== 0o400 || file.size > 65536) throw failed()
      const buffer = Buffer.alloc(65537)
      const size = readSync(fd, buffer, 0, buffer.length, 0)
      if (size > 65536) throw failed()
      return buffer.subarray(0, size)
    } finally { closeSync(fd) }
  }
  const originalInput = protectedInput()
  const input = validateInput(JSON.parse(originalInput.toString('utf8')))
  if (input.origin !== expectedOrigin) throw failed()
  console.log('Windmill bootstrap stage: protected input validated.')
  const base = `http://127.0.0.1:${port}/api`
  const call = async (method: string, route: string, data?: unknown, token?: string, timeoutMs = 10000) => {
    const headers: Record<string, string> = {}
    if (data !== undefined) headers['Content-Type'] = 'application/json'
    if (token) headers.Authorization = `Bearer ${token}`
    const response = await fetch(base + route, { method, headers, body: data === undefined ? undefined : JSON.stringify(data), redirect: 'error', signal: AbortSignal.timeout(Math.max(1, Math.floor(timeoutMs))) })
    if (!response.body) throw failed()
    const reader = response.body.getReader(); const chunks: Uint8Array[] = []; let size = 0
    try {
      while (true) {
        const { done, value } = await reader.read(); if (done) break
        size += value.length; if (size > 1048576) throw failed(); chunks.push(value)
      }
    } finally { await reader.cancel() }
    return { status: response.status, body: Buffer.concat(chunks).toString('utf8') }
  }
  const login = (password: string) => call('POST', '/auth/login', { email: 'admin@windmill.dev', password })
  stage = 'factory credential rotation'
  let session = await login(input.adminPassword)
  if (session.status !== 200) {
    session = await login('changeme')
    if (session.status !== 200) throw failed()
    if ((await call('POST', '/users/setpassword', { password: input.adminPassword }, session.body)).status !== 200) throw failed()
    session = await login(input.adminPassword)
  }
  if (session.status !== 200 || !session.body || session.body.length > 4096 || /[\r\n]/.test(session.body)) throw failed()
  const token = session.body
  if ((await login('changeme')).status === 200) throw failed()
  const settings: Record<string, unknown> = {
    require_preexisting_user_for_oauth: true, disable_stats: true, disable_hub: true,
    sync_hub_resource_types_daily: false, smtp_settings: {}, job_default_timeout: 30, base_url: input.origin,
    oauths: { keycloak: keycloakProvider(input) },
  }
  for (const [key, value] of Object.entries(settings)) {
    stage = 'instance policy ' + key
    if ((await call('POST', '/settings/global/' + key, { value }, token)).status !== 200) throw failed()
    // Secret-bearing OAuth values may be masked by the API. Authentication is
    // verified separately; critical approval/telemetry policy must read back.
    if (key !== 'oauths') {
      const check = await call('GET', '/settings/global/' + key, undefined, token)
      if (check.status !== 200 || JSON.stringify(JSON.parse(check.body)) !== JSON.stringify(value)) throw failed()
    }
  }
  stage = 'approved accounts'
  for (const user of input.approvedUsers) {
    const encoded = encodeURIComponent(user.email)
    const exists = await call('GET', '/users/exists/' + encoded, undefined, token)
    if (exists.status !== 200 || !['true', 'false'].includes(exists.body)) throw failed()
    if (exists.body === 'false' && (await call('POST', '/users/create', { email: user.email, password: randomBytes(48).toString('hex'), super_admin: user.role === 'admin', name: user.name, skip_email: true }, token)).status !== 201) throw failed()
    if ((await call('POST', '/users/update/' + encoded, { is_super_admin: user.role === 'admin', is_devops: false, disabled: false }, token)).status !== 200 ||
      (await call('POST', '/users/set_login_type/' + encoded, { login_type: 'keycloak' }, token)).status !== 200) throw failed()
  }
  stage = 'approved workspace membership'
  const workspaces = await call('GET', '/workspaces/list_as_superadmin', undefined, token)
  if (workspaces.status !== 200 || !Array.isArray(JSON.parse(workspaces.body))) throw failed()
  if (!JSON.parse(workspaces.body).some((workspace: {id:string}) => workspace.id === 'terrace')) {
    if (![200,201].includes((await call('POST', '/workspaces/create', {id:'terrace',name:'Terrace'}, token)).status)) throw failed()
  }
  if ((await call('POST', '/w/terrace/workspaces/edit_auto_invite', {operator:true,invite_all:false,auto_add:false}, token)).status !== 200) throw failed()
  for (const user of input.approvedUsers) {
    const listed = await call('GET', '/w/terrace/users/list', undefined, token)
    if (listed.status !== 200 || !Array.isArray(JSON.parse(listed.body))) throw failed()
    const found = JSON.parse(listed.body).find((member: {email:string}) => member.email === user.email)
    const role = {is_admin:user.role === 'admin',operator:user.role === 'operator'}
    if (!found) {
      if (![200,201].includes((await call('POST', '/w/terrace/workspaces/add_user', {email:user.email,...role}, token)).status)) throw failed()
    } else if ((await call('POST', '/w/terrace/users/update/' + encodeURIComponent(found.username), {...role,disabled:false}, token)).status !== 200) throw failed()
  }
  stage = 'retained account revocation'
  const accounts = await call('GET', '/users/list_as_super_admin', undefined, token)
  if (accounts.status !== 200) throw failed()
  const listed = JSON.parse(accounts.body)
  if (!Array.isArray(listed)) throw failed()
  for (const account of listed) {
    if (typeof account.email !== 'string') throw failed()
    if (account.email !== 'admin@windmill.dev' && !input.approvedUsers.some(user => user.email === account.email)) {
      // Revocation preserves every account's data; it never deletes a user.
      if ((await call('POST', '/users/update/' + encodeURIComponent(account.email), { disabled: true, is_super_admin: false, is_devops: false }, token)).status !== 200) throw failed()
    }
  }
  stage = 'Keycloak provider registration'
  const provider = await waitForKeycloak(timeout => call('GET', '/oauth/list_logins', undefined, undefined, timeout))
  if (!provider.ready) {
    console.error('Windmill provider HTTP status: ' + provider.status)
    for (const fact of providerReadbackSummary(provider.metadata)) console.error(fact)
    throw failed()
  }
  // A newer caller may have timed out waiting for this lock. Never publish
  // readiness from stale input after an operator changes the protected policy.
  stage = 'input continuity'
  if (!protectedInput().equals(originalInput)) throw failed()
  // Public marker contains no input. nginx checks only its existence.
  writeFileSync(ready, 'ready\n', { mode: 0o644, flag: 'wx' })
  console.log('Windmill approved-user bootstrap completed; factory credential rejected.')
}

if (import.meta.main) {
  console.error('Windmill launcher stage: Bun entry reached.')
  console.error('Windmill launcher argument count: ' + process.argv.length)
  try {
    for (const [name, code, label] of [['SIGTERM',143,'termination signal handler'],['SIGINT',130,'interrupt signal handler'],['SIGHUP',129,'hangup signal handler']] as const) {
      stage = label
      console.error('Windmill launcher stage: installing ' + label)
      process.on(name, () => { try { rmSync(READY, {force:true}) } finally { process.exit(code) } })
    }
    stage = 'input policy'
    if (process.argv.length !== 5 || !/^[0-9]+$/.test(process.argv[3]!) || Number(process.argv[3]) < 1024 || Number(process.argv[3]) > 65535) throw failed()
    await bootstrap(process.argv[2]!, Number(process.argv[3]), process.argv[4]!)
  } catch {
    try { rmSync(READY, {force:true}) } catch { /* Keep raw filesystem diagnostics private. */ }
    console.error('Windmill bootstrap failed at ' + stage + '; ingress remains closed. Raw diagnostics suppressed.')
    process.exitCode = 1
  }
}
