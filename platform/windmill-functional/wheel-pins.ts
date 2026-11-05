// Public PyPI metadata only; this tool never installs or imports a package.
import {readFileSync} from 'node:fs'

const rejected=()=>new Error('Windmill offline wheel metadata rejected.')
export function lockedPackages(text:string) {
  if(Buffer.byteLength(text)>8192||!text.includes('# py: 3.12')) throw rejected()
  const rows=text.split('\n').filter(line=>line&&!line.startsWith('#')).map(line=>{
    const match=/^([a-z0-9][a-z0-9-]{0,63})==([0-9][a-zA-Z0-9.]{0,31})$/.exec(line)
    if(!match) throw rejected()
    return {name:match[1]!,version:match[2]!}
  })
  if(rows.length!==20||new Set(rows.map(row=>row.name)).size!==20) throw rejected()
  return rows
}

function rank(filename:string):number {
  const parts=filename.slice(0,-4).split('-'),python=parts.at(-3),abi=parts.at(-2),platform=parts.at(-1)
  if(!filename.endsWith('.whl')||!python||!abi||!platform) return 1000
  if(['py3','py2.py3'].includes(python)&&abi==='none'&&platform==='any') return 0
  const tags=platform.split('.')
  const linux=['manylinux_2_17_x86_64','manylinux2014_x86_64','manylinux_2_28_x86_64','manylinux_2_34_x86_64']
  const target=linux.findIndex(tag=>tags.includes(tag))
  if(target<0||tags.some(tag=>!linux.includes(tag))) return 1000
  if(python==='cp312'&&abi==='cp312') return 10+target
  if(['cp39','cp310','cp311','cp312'].includes(python)&&abi==='abi3') return 20+target
  return 1000
}

export function selectedWheel(name:string,version:string,value:any) {
  if(!value||typeof value.info?.name!=='string'||value.info.name.toLowerCase().replaceAll('_','-')!==name||value.info?.version!==version||!Array.isArray(value.urls)||value.urls.length<1||value.urls.length>500) throw rejected()
  const candidates=value.urls.filter((row:any)=>row&&typeof row.filename==='string'&&rank(row.filename)<1000)
    .sort((a:any,b:any)=>rank(a.filename)-rank(b.filename)||a.filename.localeCompare(b.filename))
  if(!candidates.length) throw rejected()
  const item=candidates[0]
  if(!/^[A-Za-z0-9_.-]{1,200}$/.test(item.filename)||!item.filename.toLowerCase().startsWith(name.replaceAll('-','_')+'-'+version.toLowerCase()+'-')||typeof item.url!=='string'||!Number.isSafeInteger(item.size)||item.size<1||item.size>32*1024*1024||!/^([a-f0-9]{64})$/.test(item.digests?.sha256||'')||item.yanked!==false) throw rejected()
  let url:URL,filename:string
  try {url=new URL(item.url);filename=decodeURIComponent(url.pathname.split('/').at(-1)!)} catch {throw rejected()}
  if(url.protocol!=='https:'||url.hostname!=='files.pythonhosted.org'||url.port||url.username||url.password||url.search||url.hash||filename!==item.filename) throw rejected()
  return {name,version,filename:item.filename,url:url.href,sha256:item.digests.sha256,size:item.size}
}

async function metadata(name:string,version:string) {
  const response=await fetch(`https://pypi.org/pypi/${name}/${version}/json`,{redirect:'error',signal:AbortSignal.timeout(10000)})
  if(response.status!==200||!response.body) throw rejected()
  const reader=response.body.getReader(),chunks:Uint8Array[]=[];let bytes=0
  try {
    while(true) {
      const {done,value}=await reader.read();if(done)break
      bytes+=value.length;if(bytes>2*1024*1024)throw rejected();chunks.push(value)
    }
  } finally {await reader.cancel()}
  return selectedWheel(name,version,JSON.parse(Buffer.concat(chunks).toString()))
}

if(import.meta.main) {
  try {
    if(process.argv.length!==2) throw rejected()
    const packages=lockedPackages(readFileSync(new URL('../../windmill/f/Membership/merge_records_to_google_sheet.script.lock',import.meta.url),'utf8'))
    const wheels:ReturnType<typeof selectedWheel>[]=[]
    for(let at=0;at<packages.length;at+=4) wheels.push(...await Promise.all(packages.slice(at,at+4).map(row=>metadata(row.name,row.version))))
    console.log(JSON.stringify({schema:1,python:'3.12',wheels},null,2))
  } catch {
    console.error('Windmill offline wheel metadata rejected; diagnostics suppressed.')
    process.exitCode=1
  }
}
