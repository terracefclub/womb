import { isKeycloakProviderSet } from './policy'

// v1.839 main.rs polls settings invalidations every10s by default. Cover more
// than one refresh while keeping ingress closed; never accept a late result.
export async function waitForKeycloak(
  read: (timeoutMs: number) => Promise<{status: number; body: string}>,
  now = () => performance.now(),
  pause = (ms: number) => Bun.sleep(ms),
) {
  const deadline = now() + 30000
  let status = 0
  let metadata: unknown
  while (now() < deadline) {
    const response = await read(Math.min(10000, deadline - now()))
    status = response.status
    try { metadata = JSON.parse(response.body) } catch { metadata = undefined }
    if (now() < deadline && status === 200 && isKeycloakProviderSet(metadata)) return {ready: true, status, metadata}
    const remaining = deadline - now()
    if (remaining > 0) await pause(Math.min(250, remaining))
  }
  return {ready: false, status, metadata}
}
