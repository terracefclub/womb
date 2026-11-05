import {test,expect} from 'bun:test'
import {approvedFixtureUsers} from '../../platform/windmill-functional/functional-policy'
import {validateInput} from '../../platform/windmill/policy'

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
