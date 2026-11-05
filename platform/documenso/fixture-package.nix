{
  pkgs,
  contract,
}:
pkgs.runCommand "documenso-functional-fixture" {} ''
  mkdir -p $out
  cp -R ${contract}/. $out/
  chmod -R u+w $out
  cp ${./functional-fixture.ts} $out/functional-fixture.ts
  cp ${./client.ts} $out/client.ts
  cp ${./inputs.ts} $out/inputs.ts
  cp ${./write-inputs.ts} $out/write-inputs.ts
''
