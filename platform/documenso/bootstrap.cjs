// Uses the image's pinned generated Prisma client, not a parallel application schema.
const fs = require('node:fs')
;(async () => {
  let prisma
  try {
    const { PrismaClient } = require('/app/node_modules/@prisma/client')
    const { hash, compare } = require('/app/node_modules/@node-rs/bcrypt')
    const accounts = JSON.parse(fs.readFileSync('/run/terrace-inputs/accounts.json', 'utf8'))
    if (!Array.isArray(accounts) || !accounts.length || accounts.length > 33) throw Error()
    prisma = new PrismaClient()
    await prisma.$transaction(async tx => {
      for (const account of accounts) {
        const existing = await tx.user.findUnique({ where: { email: account.email } })
        if (existing && existing.source !== 'terrace-approved-bootstrap-v1') throw Error()
        const samePassword = account.password ? existing?.password && await compare(account.password, existing.password) : existing?.password === null
        if (existing) {
          if (existing.name === account.name && !existing.disabled && existing.roles.length === 1 && existing.roles[0] === account.role && existing.emailVerified && samePassword) continue
          await tx.user.update({ where: { id: existing.id }, data: {
            name: account.name, roles: [account.role], disabled: false, emailVerified: existing.emailVerified || new Date(),
            ...(samePassword ? {} : { password: account.password ? await hash(account.password, 12) : null }),
          } })
          await tx.session.deleteMany({ where: { userId: existing.id } })
        } else await tx.user.create({ data: {
          name: account.name, email: account.email, roles: [account.role],
          password: account.password ? await hash(account.password, 12) : null,
          emailVerified: new Date(), source: 'terrace-approved-bootstrap-v1',
        } })
      }
      const removed = await tx.user.findMany({ where: { source: 'terrace-approved-bootstrap-v1', email: { notIn: accounts.map(account => account.email) } }, select: { id: true } })
      for (const { id } of removed) {
        await tx.user.updateMany({ where: { id, disabled: false }, data: { disabled: true } })
        await tx.session.deleteMany({ where: { userId: id } })
      }
    }, { timeout: 30000 })
    console.log('Documenso approved account bootstrap complete.')
  } catch {
    console.error('Documenso approved account bootstrap failed; private diagnostics suppressed.')
    process.exitCode = 1
  } finally {
    if (prisma) try { await prisma.$disconnect() } catch {
      console.error('Documenso bootstrap database cleanup failed; private diagnostics suppressed.')
      process.exitCode = 1
    }
  }
})()
