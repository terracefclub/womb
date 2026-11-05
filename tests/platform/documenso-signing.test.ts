import { expect, test } from 'bun:test'
import { signingFingerprint, signingPassphrase } from '../../platform/documenso/signing-inputs'

test('signing passphrases are bounded and cannot inject environment or command text', () => {
  const secret = 'synthetic-passphrase_'.repeat(3)
  expect(signingPassphrase(Buffer.from(secret))).toBe(secret)
  expect(signingPassphrase(Buffer.from(secret + '\n'))).toBe(secret)
  for (const input of ['', 'a'.repeat(31), 'a'.repeat(257), secret + '\n\n', secret + '\r\n', secret + '\nINJECTED=value', secret + '\0', secret + '"', secret + '$()']) {
    expect(() => signingPassphrase(Buffer.from(input))).toThrow('Documenso signing inputs rejected.')
  }
})
test('certificate and passphrase changes invalidate the prepared signing fingerprint', () => {
  const certificate = Buffer.alloc(256, 1), passphrase = 'synthetic-passphrase_'.repeat(3)
  const original = signingFingerprint(certificate, passphrase)
  expect(original).toMatch(/^[0-9a-f]{64}$/)
  expect(signingFingerprint(Buffer.from(certificate), passphrase)).toBe(original)
  expect(signingFingerprint(Buffer.alloc(256, 2), passphrase)).not.toBe(original)
  expect(signingFingerprint(certificate, passphrase + 'x')).not.toBe(original)
  for (const size of [0, 255, 65537]) expect(() => signingFingerprint(Buffer.alloc(size), passphrase)).toThrow('Documenso signing inputs rejected.')
})
test('invalid passphrases have static errors without private excerpts', () => {
  const marker = 'PRIVATE_SIGNING_MARKER: must not be emitted'
  try { signingPassphrase(Buffer.from(marker)); throw new Error('Expected rejection') }
  catch (error) { expect((error as Error).message).toBe('Documenso signing inputs rejected.'); expect((error as Error).message).not.toContain(marker) }
})
