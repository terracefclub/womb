{
  pkgs,
  contract,
}:
pkgs.runCommand "windmill-functional-fixture" {} ''
  mkdir -p $out
  cp -R ${contract}/. $out/
  chmod -R u+w $out
  cp ${./functional-fixture.ts} $out/functional-fixture.ts
  cp ${./write-inputs.ts} $out/write-inputs.ts
  cp ${../windmill/policy.ts} $out/policy.ts
  cp ${./functional-policy.ts} $out/functional-policy.ts
''
