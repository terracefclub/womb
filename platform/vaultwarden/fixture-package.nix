# Test-only writer artifact. The identity contract supplies the reviewed,
# hash-gated producer; importing its component writer has no runtime I/O.
{
  pkgs,
  contract,
}:
pkgs.runCommand "vaultwarden-functional-fixture" {} ''
  mkdir $out
  cp -R ${contract}/. $out/
  chmod -R u+w $out
  cp ${./functional-fixture.ts} $out/functional-fixture.ts
  cp ${./policy.ts} $out/policy.ts
''
