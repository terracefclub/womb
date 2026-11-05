// Runtime-only signing inputs: no key/passphrase enters Nix, argv or diagnostics.
import { closeSync, constants, fstatSync, openSync, readSync, writeFileSync, chownSync, chmodSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { join } from 'node:path'

const rejected = () => new Error('Documenso signing inputs rejected.')
export function signingPassphrase(value: Uint8Array): string {
  const text = Buffer.from(value).toString('utf8')
  if (!/^[A-Za-z0-9_+/=-]{32,256}\n?$/.test(text)) throw rejected()
  return text.replace(/\n$/, '')
}
export function signingFingerprint(certificate: Uint8Array, passphrase: string) {
  if (certificate.length < 256 || certificate.length > 65536) throw rejected()
  return createHash('sha256').update(certificate).update('\0').update(passphrase).digest('hex')
}
function protectedFile(path: string, maximum: number, owner = 0, mode = 0o400) {
  const descriptor = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
  try {
    const stat = fstatSync(descriptor)
    if (!stat.isFile() || stat.uid !== owner || (stat.mode & 0o777) !== mode || stat.size > maximum) throw rejected()
    const content = Buffer.alloc(maximum + 1)
    let length = 0
    while (length <= maximum) {
      const count = readSync(descriptor, content, length, content.length - length, null)
      if (!count) break
      length += count
    }
    if (length > maximum) throw rejected()
    return content.subarray(0, length)
  } finally { closeSync(descriptor) }
}
if (import.meta.main) {
  try {
    const [mode, source, output] = process.argv.slice(2)
    if (process.argv.length !== 5 || !['prepare', 'check'].includes(mode!)) throw rejected()
    const certificate = protectedFile(join(source!, 'signing.p12'), 65536)
    const passphrase = signingPassphrase(protectedFile(join(source!, 'signing-passphrase'), 257))
    const fingerprint = signingFingerprint(certificate, passphrase)
    if (mode === 'check') {
      if (protectedFile(join(output!, 'signing.sha256'), 65, 0, 0o600).toString('utf8') !== fingerprint + '\n' || protectedFile(join(output!, 'signing.env'), 300, 0, 0o600).toString('utf8') !== `NEXT_PRIVATE_SIGNING_PASSPHRASE=${passphrase}\n`) throw rejected()
      if (signingFingerprint(protectedFile(join(output!, 'signing.p12'), 65536, 1001), passphrase) !== fingerprint) throw rejected()
    } else {
      const write = (name: string, value: string | Uint8Array, mode: number) => {
        const path = join(output!, name)
        writeFileSync(path, value, { mode }); chmodSync(path, mode)
      }
      write('signing.p12', certificate, 0o400)
      // Host parent remains root0700; the single read-only container bind is UID1001.
      chownSync(join(output!, 'signing.p12'), 1001, 1001)
      write('signing-passphrase', passphrase, 0o400)
      write('signing.env', `NEXT_PRIVATE_SIGNING_PASSPHRASE=${passphrase}\n`, 0o600)
      write('signing.sha256', fingerprint + '\n', 0o600)
    }
  } catch {
    console.error('Documenso signing preparation failed; private diagnostics suppressed.')
    process.exitCode = 1
  }
}
