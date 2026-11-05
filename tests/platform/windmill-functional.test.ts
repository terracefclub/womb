import {test,expect} from 'bun:test'
import {approvedFixtureUsers} from '../../platform/windmill-functional/functional-policy'
import {validateInput} from '../../platform/windmill/policy'
import {mkdtempSync,mkdirSync,readFileSync,writeFileSync,copyFileSync,rmSync,lstatSync,readdirSync} from 'node:fs'
import {tmpdir} from 'node:os'
import {join} from 'node:path'
import {pathToFileURL} from 'node:url'

test('fixture approval never follows unknown or editorial-only IdP enrollment',()=>{
  const names=['local-member','local-alumni','local-editorial-admin','local-unapproved','local-editorial-editor']
  const users=names.map(username=>({username,email:username+'@terrace.invalid'}))
  const approvedUsers=approvedFixtureUsers(users)
  const result=validateInput({adminPassword:'a'.repeat(40),origin:'https://windmill.terrace.invalid',
    issuer:'https://identity.terrace.invalid/realms/terrace',clientId:'terrace-windmill',clientSecret:'b'.repeat(40),approvedUsers})
  expect(result.approvedUsers.map(u=>u.role)).toEqual(['operator','operator','admin'])
  expect(result.approvedUsers.some(u=>u.email===users[3]!.email||u.email===users[4]!.email)).toBe(false)
  expect(()=>approvedFixtureUsers(users.filter(u=>u.username!=='local-member'))).toThrow('Windmill functional fixture rejected.')
  expect(()=>approvedFixtureUsers([...users,users[0]!])).toThrow('Windmill functional fixture rejected.')
})

test('shared input writer is import-safe, exclusive and excludes server secrets from browser data',async()=>{
  const root=mkdtempSync(join(tmpdir(),'windmill-writer-'))
  try {
    const module=join(root,'package');mkdirSync(module,{mode:0o700})
    for(const [name,source] of [
      ['write-inputs.ts','platform/windmill-functional/write-inputs.ts'],
      ['functional-policy.ts','platform/windmill-functional/functional-policy.ts'],
      ['policy.ts','platform/windmill/policy.ts'],
    ]) copyFileSync(new URL('../../'+source,import.meta.url),join(module,name!))
    const before=readdirSync(root)
    const {writeWindmillInputs}=await import(pathToFileURL(join(module,'write-inputs.ts')).href)
    expect(readdirSync(root)).toEqual(before)
    const directory=join(root,'identity');mkdirSync(directory,{mode:0o700})
    writeFileSync(join(directory,'terrace-realm.json'),'{"realm":"synthetic","users":[]}')
    writeFileSync(join(directory,'db-password'),'synthetic-database-input-only')
    const fixture={directory,issuer:'https://identity.terrace.invalid/realms/terrace',extraSecrets:{windmill:'c'.repeat(40)},
      users:['local-member','local-alumni','local-editorial-admin','local-unapproved'].map(username=>({username,email:username+'@terrace.invalid',password:'synthetic-idp-test-input'}))}
    const output=join(root,'windmill')
    writeWindmillInputs(fixture,output)
    const input=JSON.parse(readFileSync(join(output,'bootstrap.json'),'utf8'))
    const browser=JSON.parse(readFileSync(join(directory,'windmill-browser.json'),'utf8'))
    expect(input.approvedUsers.map((user:{role:string})=>user.role)).toEqual(['operator','operator','admin'])
    expect(Object.keys(browser)).toEqual(['users'])
    expect(Object.keys(browser.users[0]).sort()).toEqual(['email','password','username'])
    expect(JSON.stringify(browser).includes(input.adminPassword)||JSON.stringify(browser).includes(input.clientSecret)).toBe(false)
    expect((lstatSync(output).mode&0o777)===0o700).toBe(true)
    for(const name of ['bootstrap.json','terrace-realm.json','db-password']) expect((lstatSync(join(output,name)).mode&0o777)===0o400).toBe(true)
    const bytes=readFileSync(join(output,'bootstrap.json'))
    expect(()=>writeWindmillInputs(fixture,output)).toThrow('Windmill fixture inputs rejected; private diagnostics suppressed.')
    expect(readFileSync(join(output,'bootstrap.json')).equals(bytes)).toBe(true)
  } finally {rmSync(root,{recursive:true,force:true})}
})
