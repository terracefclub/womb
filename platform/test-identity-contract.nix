{
  pkgs,
  tfcSource,
}: let
  # Only these three reviewed, secret-free producer files enter the store.
  # All generated realm secrets and fixture accounts remain runtime-only.
  checked = relative: hash: let
    path = tfcSource + "/" + relative;
  in
    if builtins.hashFile "sha256" path != hash
    then throw "Gateway check requires the reviewed TFC identity contract sources."
    else
      builtins.path {
        inherit path;
        name = builtins.baseNameOf relative;
      };
  realm = checked "identity/tools/production-realm.ts" "91ff97ec6d0902e085a29cf3e96349ee7ef99c21eabffedc033543b67931603e";
  clients = checked "identity/tools/realm-config.ts" "0df304749356778154387550267dfea1dcd232be0682eeb4e07a5b52016810d3";
  input = checked "cms/src/auth/production-input.ts" "59f359df123bda17222684d45cdd0b9ca2c65423e4274ad32dc67856a4870f9d";
in
  pkgs.runCommand "terrace-gateway-test-contract" {} ''
    mkdir -p $out/identity/tools $out/cms/src/auth
    cp ${realm} $out/identity/tools/production-realm.ts
    cp ${clients} $out/identity/tools/realm-config.ts
    cp ${input} $out/cms/src/auth/production-input.ts
    cp ${./website-gateway/fixture.ts} $out/fixture.ts
    cp ${./website-gateway/fixture-clients.ts} $out/fixture-clients.ts
  ''
