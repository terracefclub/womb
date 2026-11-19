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
  # The same pins as the production producer (identity-contract.nix).
  pins = builtins.fromJSON (builtins.readFile ./identity-contract.json);
  realm = checked "identity/tools/production-realm.ts" pins."identity/tools/production-realm.ts";
  clients = checked "identity/tools/realm-config.ts" pins."identity/tools/realm-config.ts";
  input = checked "cms/src/auth/production-input.ts" pins."cms/src/auth/production-input.ts";
in
  pkgs.runCommand "terrace-gateway-test-contract" {} ''
    mkdir -p $out/identity/tools $out/cms/src/auth
    cp ${realm} $out/identity/tools/production-realm.ts
    cp ${clients} $out/identity/tools/realm-config.ts
    cp ${input} $out/cms/src/auth/production-input.ts
    cp ${./website-gateway/fixture.ts} $out/fixture.ts
    cp ${./website-gateway/fixture-clients.ts} $out/fixture-clients.ts
  ''
