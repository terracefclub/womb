// Guest-only security regression using the image's exact RR7 decoder. No data emitted.
;(async () => {
  const deadline = setTimeout(() => {
    console.error('Admin loader security check expired; response diagnostics suppressed.')
    process.exit(1)
  }, 20000)
  try {
    const { UNSAFE_decodeViaTurboStream: decode, UNSAFE_SingleFetchRedirectSymbol: redirect } = require('/app/node_modules/react-router')
    const origin = 'https://documents.terrace.invalid'
    const safeRedirect = value => {
      const url = new URL(value, origin)
      return url.origin === origin && ['/', '/signin'].includes(url.pathname)
    }
    for (const path of ['/admin/users', '/admin/users.data?_routes=routes%2F_authenticated%2B%2Fadmin%2B%2Fusers._index']) {
      const response = await fetch(origin + path, { redirect: 'manual', signal: AbortSignal.timeout(10000) })
      const chunks = [], reader = response.body.getReader()
      let size = 0
      try {
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          size += value.byteLength
          if (size > 65536) throw Error()
          chunks.push(value)
        }
      } finally { await reader.cancel() }
      const body = Buffer.concat(chunks)
      for (const forbidden of ['@terrace.invalid', '@documents.terrace.invalid', '"users"', '"emailVerified"', '"password"']) {
        if (body.includes(forbidden)) throw Error()
      }
      if (!path.includes('.data')) {
        if (response.status !== 302 || !safeRedirect(response.headers.get('Location'))) throw Error()
        continue
      }
      // RR7 deliberately represents a loader redirect as 202, not a browser 3xx.
      // Only its exact redirect-only decoded result is accepted; no route data.
      if (response.status !== 202 || !response.headers.get('Content-Type')?.startsWith('text/x-script')) throw Error()
      const decoded = await decode(new Response(body).body, globalThis)
      await decoded.done
      const value = decoded.value
      if (!value || Reflect.ownKeys(value).length !== 1 || Reflect.ownKeys(value)[0] !== redirect) throw Error()
      const target = value[redirect]
      if (!target || Object.keys(target).sort().join(',') !== 'redirect,reload,replace,revalidate,status' || target.status !== 302 || !safeRedirect(target.redirect) || [target.reload, target.replace, target.revalidate].some(value => typeof value !== 'boolean')) throw Error()
    }
    console.log('Admin page and selected data loader denied access without user data.')
  } catch {
    console.error('Admin loader security regression failed; response diagnostics suppressed.')
    process.exitCode = 1
  } finally { clearTimeout(deadline) }
})()
