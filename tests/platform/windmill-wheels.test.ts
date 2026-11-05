import {expect,test} from 'bun:test'
import {lockedPackages,selectedWheel} from '../../platform/windmill-functional/wheel-pins'
import {readFileSync} from 'node:fs'
import pins from '../../platform/windmill-functional/membership-wheels.json'

const name='sample-package',version='1.2.3'
function row(filename='sample_package-1.2.3-py3-none-any.whl') {
  return {filename,url:'https://files.pythonhosted.org/packages/aa/'+filename,size:1234,digests:{sha256:'a'.repeat(64)},yanked:false}
}
const metadata=(urls:any[])=>({info:{name,version},urls})

test('wheel selection pins exact package/version/host/hash and prefers universal over compatible Linux',()=>{
  const binary=row('sample_package-1.2.3-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl')
  expect(selectedWheel(name,version,metadata([binary,row()])).filename).toBe(row().filename)
  expect(selectedWheel(name,version,metadata([binary])).filename).toBe(binary.filename)
  const abi3=row('sample_package-1.2.3-cp311-abi3-manylinux_2_28_x86_64.whl')
  expect(selectedWheel(name,version,metadata([abi3])).filename).toBe(abi3.filename)
})

test('other platforms, yanked releases, redirects/credentials, malformed or oversized metadata fail closed',()=>{
  for(const filename of ['sample_package-1.2.3-cp313-cp313-manylinux_2_28_x86_64.whl','sample_package-1.2.3-py3-none-win_amd64.whl','sample_package-1.2.3-cp312-cp312-manylinux_2_28_aarch64.whl']) {
    expect(()=>selectedWheel(name,version,metadata([row(filename)]))).toThrow('Windmill offline wheel metadata rejected.')
  }
  for(const change of [{yanked:true},{size:33554433},{size:0},{digests:{sha256:'private-canary'}},
    {url:'https://private-canary@files.pythonhosted.org/file.whl'},{url:'https://outside.invalid/private-canary'},
    {url:row().url+'?private-canary'},{url:'https://files.pythonhosted.org/%E0'},{filename:'other-1.2.3-py3-none-any.whl'}]) {
    expect(()=>selectedWheel(name,version,metadata([{...row(),...change}]))).toThrow('Windmill offline wheel metadata rejected.')
  }
  expect(()=>selectedWheel(name,version,{info:{name,version},urls:Array(501).fill(row())})).toThrow('Windmill offline wheel metadata rejected.')
  expect(()=>selectedWheel(name,version,metadata([]))).toThrow('Windmill offline wheel metadata rejected.')
})

test('only the bounded exact Python3.12 lock rows enter public metadata lookup',()=>{
  const lock='# py: 3.12\n'+Array.from({length:20},(_,i)=>`package-${i}==1.2.3`).join('\n')
  expect(lockedPackages(lock).length).toBe(20)
  for(const text of [lock.replace('3.12','3.13'),lock+'\npackage-0==1.2.3',lock.replace('package-0==1.2.3','https://private.invalid'),lock+' '.repeat(8193)]) {
    expect(()=>lockedPackages(text)).toThrow('Windmill offline wheel metadata rejected.')
  }
})

test('committed wheel descriptors match every exact original lock row',()=>{
  const rows=lockedPackages(readFileSync(new URL('../../windmill/f/Membership/merge_records_to_google_sheet.script.lock',import.meta.url),'utf8'))
  expect(pins.schema===1&&pins.python==='3.12').toBe(true)
  expect(pins.wheels.map(({name,version})=>({name,version}))).toEqual(rows)
  for(const wheel of pins.wheels) {
    const candidate={...wheel,digests:{sha256:wheel.sha256},yanked:false}
    expect(selectedWheel(wheel.name,wheel.version,{info:wheel,urls:[candidate]})).toEqual(wheel)
  }
})


test('exported platform SDK lock is byte-identical to the original workflow lock', () => {
  const original = readFileSync(new URL('../../windmill/f/Membership/merge_records_to_google_sheet.script.lock', import.meta.url));
  const packaged = readFileSync(new URL('../../platform/windmill-functional/membership-script.lock', import.meta.url));
  expect(packaged.equals(original)).toBe(true);
});
