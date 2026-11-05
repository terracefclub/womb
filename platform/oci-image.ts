// Official public metadata only. Registry bearer tokens never leave this function.
const repositories = new Set(['windmill-labs/windmill', 'documenso/documenso'])
const digestPattern = /^sha256:[a-f0-9]{64}$/
export const digest = (bytes: Uint8Array | string) => 'sha256:' + new Bun.CryptoHasher('sha256').update(bytes).digest('hex')
const rejected = () => new Error('Official image metadata rejected.')

export async function inspectImage(repository: string, version: string, request: typeof fetch = fetch) {
  try {
    if (!repositories.has(repository) || !/^v?[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$/.test(version)) throw rejected()
    const read = async (url: string, headers: Record<string, string>, maximum: number, blob = false) => {
      let response = await request(url, { headers, redirect: blob ? 'manual' : 'error', signal: AbortSignal.timeout(30000) })
      if (blob && [302, 307].includes(response.status)) {
        const location = response.headers.get('location')
        await response.body?.cancel()
        if (!location) throw rejected()
        const target = new URL(location)
        if (target.protocol !== 'https:' || target.hostname !== 'pkg-containers.githubusercontent.com' || target.port || target.username || target.password) throw rejected()
        // One GHCR blob-CDN hop only. Never forward the registry bearer token.
        response = await request(target, { headers: {}, redirect: 'error', signal: AbortSignal.timeout(30000) })
      }
      if (!response.ok || !response.body) { await response.body?.cancel(); throw rejected() }
      const reader = response.body.getReader()
      const chunks: Uint8Array[] = []
      let size = 0
      try {
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          size += value.length
          if (size > maximum) throw rejected()
          chunks.push(value)
        }
      } finally { await reader.cancel() }
      const bytes = Buffer.concat(chunks)
      return { response, bytes, value: JSON.parse(bytes.toString('utf8')) }
    }
    const scope = encodeURIComponent(`repository:${repository}:pull`)
    const authorization = await read(`https://ghcr.io/token?service=ghcr.io&scope=${scope}`, {}, 32768)
    const token = authorization.value.token
    if (typeof token !== 'string' || !token || token.length > 30000 || /[\r\n]/.test(token)) throw rejected()
    const headers = { Authorization: `Bearer ${token}`, Accept: 'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json' }
    const manifest = async (reference: string) => {
      const data = await read(`https://ghcr.io/v2/${repository}/manifests/${reference}`, headers, 1048576)
      const declared = data.response.headers.get('docker-content-digest')
      if (!declared || !digestPattern.test(declared) || digest(data.bytes) !== declared || (reference.startsWith('sha256:') && declared !== reference)) throw rejected()
      return { ...data, declared }
    }
    let image = await manifest(version)
    if (image.value.manifests) {
      if (!Array.isArray(image.value.manifests)) throw rejected()
      const matches = image.value.manifests.filter((entry: { platform?: { architecture?: string; os?: string } }) => entry?.platform?.architecture === 'amd64' && entry.platform.os === 'linux')
      if (matches.length !== 1 || !digestPattern.test(matches[0].digest ?? '')) throw rejected()
      image = await manifest(matches[0].digest)
    }
    if (!digestPattern.test(image.value.config?.digest ?? '') || !Array.isArray(image.value.layers)) throw rejected()
    const config = await read(`https://ghcr.io/v2/${repository}/blobs/${image.value.config.digest}`, { Authorization: headers.Authorization }, 1048576, true)
    if (digest(config.bytes) !== image.value.config.digest || config.value.architecture !== 'amd64' || config.value.os !== 'linux') throw rejected()
    return { imageName: `ghcr.io/${repository}`, version, imageDigest: image.declared, os: 'linux', arch: 'amd64' }
  } catch { throw rejected() }
}

if (import.meta.main) {
  try {
    if (process.argv.length !== 4) throw rejected()
    console.log(JSON.stringify(await inspectImage(process.argv[2]!, process.argv[3]!), null, 2))
  } catch {
    // Parse/network exceptions can contain bearer tokens, response bodies or URLs.
    console.error('Official image metadata inspection failed; raw diagnostics suppressed.')
    process.exitCode = 1
  }
}
