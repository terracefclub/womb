// Disposable guests only; generated credentials never enter the store or output.
import { mkdirSync, writeFileSync, readFileSync } from 'node:fs'
import { randomBytes } from 'node:crypto'
import { createIdentityFixture } from './fixture'
import { windmillClient, validateInput } from './policy'
import { approvedFixtureUsers } from './functional-policy'

if (import.meta.main) {
  try {
    const fixture = createIdentityFixture({extraSecretNames:['windmill'],
      extraClientFactories:[secrets => windmillClient('https://windmill.terrace.invalid',secrets.windmill!)]})
    const directory = '/var/lib/terrace-windmill-inputs'
    mkdirSync(directory,{recursive:true,mode:0o700})
    const write = (name:string,value:string|Buffer) => writeFileSync(`${directory}/${name}`,value,{mode:0o400,flag:'wx'})
    for (const name of ['terrace-realm.json','db-password']) write(name,readFileSync(`${fixture.directory}/${name}`))
    const input=validateInput({adminPassword:randomBytes(40).toString('hex'),origin:'https://windmill.terrace.invalid',
      issuer:fixture.issuer,clientId:'terrace-windmill',clientSecret:fixture.extraSecrets.windmill,
      approvedUsers:approvedFixtureUsers(fixture.users)})
    write('bootstrap.json',JSON.stringify(input))
    // No instance recovery password, OIDC secret or temporary IdP admin goes to the browser.
    writeFileSync(`${fixture.directory}/windmill-browser.json`,JSON.stringify({
      users:fixture.users.map(({username,email,password})=>({username,email,password})),
    }),{mode:0o400,flag:'wx'})
  } catch {
    console.error('Windmill functional fixture preparation failed; private diagnostics suppressed.')
    process.exitCode=1
  }
}
