// Test-only composition of the reviewed user-free identity producer.
import {chmodSync,mkdirSync,readFileSync,writeFileSync} from 'node:fs'
import {randomBytes} from 'node:crypto'
import {createIdentityFixture} from './fixture'
import {validateInput,vaultwardenClient} from './policy'
const root='/var/lib/terrace-vaultwarden-inputs'
export function writeVaultwardenInputs(fixture: ReturnType<typeof createIdentityFixture>) {
  if (fixture.directory!=='/var/lib/terrace-gateway-test') throw Error('Synthetic Vaultwarden fixture rejected.')
  const secret=()=>randomBytes(40).toString('base64url')
  const names=['local-member','local-alumni','local-unapproved']
  const users=names.map(username=>{
    const user=fixture.users.find(user=>user.username===username)
    if (!user) throw Error('Synthetic Vaultwarden fixture rejected.')
    return {username:user.username,email:user.email,password:user.password,masterPassword:secret()}
  })
  const input=validateInput({origin:'https://vault.terrace.invalid',issuer:fixture.issuer,clientId:'terrace-vaultwarden',clientSecret:fixture.extraSecrets.vaultwarden,adminToken:secret(),approvedEmails:users.filter(user=>user.username!=='local-unapproved').map(user=>user.email)})
  mkdirSync(root,{recursive:true,mode:0o700})
  writeFileSync(`${root}/input.json`,JSON.stringify(input),{mode:0o400,flag:'wx'})
  // No database/client/admin material goes to the browser.
  const browserFile=`${fixture.directory}/vaultwarden-browser.json`
  writeFileSync(browserFile,JSON.stringify({users}),{mode:0o400,flag:'wx'})
  return {inputFile:`${root}/input.json`,browserFile}
}
if (import.meta.main) try {
  if (['revoke','restore'].includes(process.argv[2]!)) {
    if (process.argv.length!==3) throw Error()
    const path=`${root}/input.json`, input=validateInput(JSON.parse(readFileSync(path,'utf8')))
    input.approvedEmails=process.argv[2] === 'restore' ? ['local-member@terrace.invalid','local-alumni@terrace.invalid'] : input.approvedEmails.filter(email=>email!=='local-alumni@terrace.invalid')
    validateInput(input)
    writeFileSync(path,JSON.stringify(input));chmodSync(path,0o400)
  } else {
    if (process.argv.length!==2) throw Error()
    const fixture=createIdentityFixture({extraSecretNames:['vaultwarden'],extraClientFactories:[secrets=>vaultwardenClient('https://vault.terrace.invalid',secrets.vaultwarden!)]})
    writeVaultwardenInputs(fixture)
  }
} catch {console.error('Disposable Vaultwarden identity preparation failed; private diagnostics suppressed.');process.exitCode=1}
