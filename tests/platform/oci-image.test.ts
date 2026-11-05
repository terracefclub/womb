import { expect, test } from 'bun:test'
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { digest, inspectImage } from '../../platform/oci-image'

function fixture(options: { arch?: string; corruptManifest?: boolean; duplicate?: boolean; badConfig?: boolean; httpError?: boolean; oversized?: boolean; redirect?: boolean; attacker?: boolean; nested?: boolean } = {}) {
  const token = 'PRIVATE_REGISTRY_TOKEN_MARKER'
  const config = JSON.stringify({ architecture: options.arch ?? 'amd64', os: 'linux' })
  const image = JSON.stringify({ config: { digest: options.badConfig ? digest('wrong') : digest(config) }, layers: [] })
  const index = JSON.stringify({ manifests: [
    { digest: digest('arm'), platform: { architecture: 'arm64', os: 'linux' } },
    ...Array.from({ length: options.duplicate ? 2 : 1 }, () => ({ digest: digest(image), platform: { architecture: 'amd64', os: 'linux' } })),
  ] })
  const calls: string[] = []
  const request = (async (url: string | URL | Request, init: RequestInit) => {
    calls.push(String(url))
    expect(init.redirect).toBe(String(url).includes('/blobs/') ? 'manual' : 'error')
    expect(init.signal).toBeInstanceOf(AbortSignal)
    if (String(url).includes('/token?')) return new Response(JSON.stringify({ token: options.oversized ? token.repeat(2000) : token }))
    if (String(url).startsWith('https://pkg-containers.githubusercontent.com/')) {
      expect(init.headers).toEqual({})
      return options.nested ? new Response(null, { status: 307, headers: { location: 'https://attacker.invalid/' } }) : new Response(config)
    }
    expect((init.headers as Record<string, string>).Authorization).toBe(`Bearer ${token}`)
    if (options.httpError) return new Response('PRIVATE_RESPONSE_MARKER', { status: 401 })
    if (String(url).includes('/blobs/')) return options.redirect ? new Response(null, { status: 307, headers: { location: `https://${options.attacker ? 'attacker.invalid' : 'pkg-containers.githubusercontent.com'}/blob?signature=PRIVATE_URL_MARKER` } }) : new Response(config)
    const body = String(url).endsWith('/1.839.0') || String(url).endsWith('/v2.21.1') ? index : image
    return new Response(body, { headers: { 'docker-content-digest': options.corruptManifest ? digest('tampered') : digest(body) } })
  }) as typeof fetch
  return { request, calls, token, expected: digest(image) }
}

test('selects and verifies actual Linux amd64 manifest/config without returning a token', async () => {
  const fake = fixture()
  const result = await inspectImage('windmill-labs/windmill', '1.839.0', fake.request)
  expect(result).toEqual({ imageName: 'ghcr.io/windmill-labs/windmill', version: '1.839.0', imageDigest: fake.expected, os: 'linux', arch: 'amd64' })
  expect(fake.calls).toHaveLength(4)
  expect(JSON.stringify(result)).not.toContain(fake.token)
})
test('supports only the official Documenso repository and exact version', async () => {
  const fake = fixture()
  expect((await inspectImage('documenso/documenso', 'v2.21.1', fake.request)).imageDigest).toBe(fake.expected)
})
test('invalid repository/tag starts no request', async () => {
  const fake = fixture()
  for (const [repository, version] of [['attacker/image', '1.839.0'], ['windmill-labs/windmill', 'latest'], ['windmill-labs/windmill', '1.839.0?private']]) {
    await expect(inspectImage(repository!, version!, fake.request)).rejects.toThrow('Official image metadata rejected.')
  }
  expect(fake.calls).toHaveLength(0)
})
for (const options of [{ corruptManifest: true }, { duplicate: true }, { arch: 'arm64' }, { badConfig: true }, { httpError: true }, { oversized: true }]) {
  test(`rejects unsafe registry metadata: ${Object.keys(options)[0]}`, async () => {
    const fake = fixture(options)
    await expect(inspectImage('windmill-labs/windmill', '1.839.0', fake.request)).rejects.toThrow('Official image metadata rejected.')
  })
}

test('one approved blob CDN hop drops registry authorization and verifies content', async () => {
  const fake = fixture({ redirect: true })
  expect((await inspectImage('windmill-labs/windmill', '1.839.0', fake.request)).imageDigest).toBe(fake.expected)
  expect(fake.calls).toHaveLength(5)
})
for (const options of [{ redirect: true, attacker: true }, { redirect: true, nested: true }]) {
  test(`rejects unsafe blob redirect: ${Object.keys(options)[1]}`, async () => {
    const fake = fixture(options)
    await expect(inspectImage('windmill-labs/windmill', '1.839.0', fake.request)).rejects.toThrow('Official image metadata rejected.')
  })
}

test('CLI rejects private-shaped invalid input without echo or network access', () => {
  const child = Bun.spawnSync([process.execPath, `${import.meta.dir}/../../platform/oci-image.ts`, 'windmill-labs/windmill', 'PRIVATE_INPUT_MARKER'])
  expect(child.exitCode).toBe(1)
  expect(child.stdout.toString()).toBe('')
  expect(child.stderr.toString()).toBe('Official image metadata inspection failed; raw diagnostics suppressed.\n')
})

test('CLI suppresses registry token and raw HTTP response diagnostics', () => {
  const temporary = mkdtempSync(join(tmpdir(), 'terrace-oci-cli-'))
  try {
    const preload = join(temporary, 'transport.ts')
    writeFileSync(preload, `let calls=0; globalThis.fetch=async()=> ++calls === 1 ? new Response(JSON.stringify({token:'PRIVATE_TOKEN_MARKER'})) : new Response('PRIVATE_RAW_RESPONSE_MARKER',{status:401});`, {mode:0o600})
    const child = Bun.spawnSync([process.execPath, '--preload', preload, `${import.meta.dir}/../../platform/oci-image.ts`, 'windmill-labs/windmill', '1.839.0'])
    expect(child.exitCode).toBe(1)
    expect(child.stdout.toString()).toBe('')
    expect(child.stderr.toString()).toBe('Official image metadata inspection failed; raw diagnostics suppressed.\n')
  } finally { rmSync(temporary, {recursive:true, force:true}) }
})
