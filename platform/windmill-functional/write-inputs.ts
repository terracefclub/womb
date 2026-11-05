// Disposable fixture composition only. Importing this module performs no IO.
import {lstatSync, mkdirSync, readFileSync, writeFileSync} from 'node:fs'
import {randomBytes} from 'node:crypto'
import type {createIdentityFixture} from '../website-gateway/fixture'
import {validateInput} from './policy'
import {approvedFixtureUsers} from './functional-policy'

export function writeWindmillInputs(fixture: ReturnType<typeof createIdentityFixture>, directory='/var/lib/terrace-windmill-inputs') {
  try {
    mkdirSync(directory,{recursive:true,mode:0o700})
    const info=lstatSync(directory)
    if(!info.isDirectory()||info.isSymbolicLink()||info.uid!==process.getuid?.()||(info.mode&0o777)!==0o700) throw new Error()
    const write=(name:string,value:string|Buffer)=>writeFileSync(`${directory}/${name}`,value,{mode:0o400,flag:'wx'})
    for(const name of ['terrace-realm.json','db-password']) write(name,readFileSync(`${fixture.directory}/${name}`))
    const input=validateInput({adminPassword:randomBytes(40).toString('hex'),origin:'https://windmill.terrace.invalid',
      issuer:fixture.issuer,clientId:'terrace-windmill',clientSecret:fixture.extraSecrets.windmill,
      approvedUsers:approvedFixtureUsers(fixture.users)})
    write('bootstrap.json',JSON.stringify(input))
    // Server/recovery secrets and the temporary IdP admin never reach browsers.
    writeFileSync(`${fixture.directory}/windmill-browser.json`,JSON.stringify({
      users:fixture.users.map(({username,email,password})=>({username,email,password})),
    }),{mode:0o400,flag:'wx'})
  } catch {
    throw new Error('Windmill fixture inputs rejected; private diagnostics suppressed.')
  }
}
