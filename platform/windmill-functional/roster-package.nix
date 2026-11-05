{pkgs}:
pkgs.runCommand "terrace-windmill-roster-contract" {nativeBuildInputs = [pkgs.python3];} ''
  mkdir "$out"
  python - <<'PY'
  import hashlib, json, os
  from pathlib import Path
  roster = Path('${../../windmill/f/Membership/record_completed_membership.py}').read_bytes().decode('utf8')
  contract = Path('${../../tests/membership/test_roster.py}').read_bytes().decode('utf8')
  lock_bytes = Path('${../../windmill/f/Membership/merge_records_to_google_sheet.script.lock}').read_bytes()
  assert lock_bytes == Path('${./membership-script.lock}').read_bytes()
  lock = lock_bytes.decode('utf8')
  wheels = json.loads(Path('${./membership-wheels.json}').read_text())['wheels']
  template = Path('${./roster-job.py}').read_text()
  for key, value in [('ROSTER_SOURCE',roster),('CONTRACT_SOURCE',contract),('LOCK_SOURCE',lock),('WHEEL_VERSIONS',{row['name']:row['version'] for row in wheels})]:
      marker = '"__TERRACE_' + key + '__"'
      assert template.count(marker) == 1
      template = template.replace(marker,json.dumps(value))
  assert '__TERRACE_' not in template
  compile(template,'<reviewed-roster-job>','exec')
  output = Path(os.environ['out'])
  (output/'roster-job.py').write_text(template)
  (output/'lock.txt').write_text(lock)
  expected = {'status':'synthetic-roster-contract-ok','tests':11,
              'sourceDigest':hashlib.sha256(roster.encode()).hexdigest(),
              'contractDigest':hashlib.sha256(contract.encode()).hexdigest(),
              'lockDigest':hashlib.sha256(lock.encode()).hexdigest()}
  (output/'manifest.json').write_text(json.dumps({'schema':1,'scriptDigest':hashlib.sha256(template.encode()).hexdigest(),'result':expected},sort_keys=True))
  PY
''
