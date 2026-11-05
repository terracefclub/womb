// Disposable runtime fixture; no credentials in the Nix store.
import {mkdirSync,writeFileSync,readFileSync,chmodSync} from 'node:fs'
import {randomBytes} from 'node:crypto'
const directory='/var/lib/terrace-vaultwarden-inputs'
try {
  if (['rotate','restore'].includes(process.argv[2]!)) {
    const path=`${directory}/input.json`, input=JSON.parse(readFileSync(path,'utf8'))
    input.approvedEmails=[process.argv[2] === 'restore' ? 'approved@terrace.invalid' : 'second@terrace.invalid']
    writeFileSync(path,JSON.stringify(input),{mode:0o400});chmodSync(path,0o400)
  } else {
    mkdirSync(directory,{mode:0o700,recursive:true})
    writeFileSync(`${directory}/input.json`,JSON.stringify({origin:'https://vault.terrace.invalid',issuer:'https://identity.terrace.invalid/realms/terrace',clientId:'terrace-vaultwarden',clientSecret:randomBytes(32).toString('hex'),adminToken:randomBytes(32).toString('hex'),approvedEmails:['approved@terrace.invalid']}),{mode:0o400,flag:'wx'})
  }
} catch {console.error('Synthetic Vaultwarden fixture operation failed.');process.exitCode=1}
