// Test enrollment is explicit; IdP roles never implicitly grant app approval.
export function approvedFixtureUsers(users: {username:string; email:string}[]) {
  return ([['local-member','operator'],['local-alumni','operator'],['local-editorial-admin','admin']] as const).map(([username,role]) => {
    const matching = users.filter(user => user.username === username)
    if (matching.length !== 1) throw new Error('Windmill functional fixture rejected.')
    return {email:matching[0]!.email,name:'Synthetic '+username,role}
  })
}
