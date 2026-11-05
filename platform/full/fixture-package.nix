{
  pkgs,
  contract,
}:
pkgs.runCommand "terrace-full-application-fixture" {} ''
  mkdir -p $out/{full,website-gateway,documenso,windmill,windmill-functional,vaultwarden}
  cp -R ${contract}/. $out/website-gateway/
  chmod -R u+w $out
  cp ${./fixture.ts} $out/full/fixture.ts
  cp ${./clients.ts} $out/full/clients.ts
  cp ${../documenso/write-inputs.ts} $out/documenso/write-inputs.ts
  cp ${../documenso/inputs.ts} $out/documenso/inputs.ts
  cp ${../documenso/client.ts} $out/documenso/client.ts
  cp ${../windmill/policy.ts} $out/windmill/policy.ts
  cp ${../windmill-functional/functional-fixture.ts} $out/windmill-functional/functional-fixture.ts
  cp ${../windmill-functional/write-inputs.ts} $out/windmill-functional/write-inputs.ts
  cp ${../windmill-functional/functional-policy.ts} $out/windmill-functional/functional-policy.ts
  ln -s ../windmill/policy.ts $out/windmill-functional/policy.ts
  cp ${../vaultwarden/functional-fixture.ts} $out/vaultwarden/functional-fixture.ts
  cp ${../vaultwarden/policy.ts} $out/vaultwarden/policy.ts
  for component in documenso windmill-functional vaultwarden; do
    ln -s ../website-gateway/fixture.ts $out/$component/fixture.ts
    ln -s ../website-gateway/fixture-clients.ts $out/$component/fixture-clients.ts
    ln -s ../website-gateway/identity $out/$component/identity
    ln -s ../website-gateway/cms $out/$component/cms
  done
''
