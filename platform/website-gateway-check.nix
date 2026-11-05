{
  webArchive,
  cmsArchive,
  tfcSource,
}: let
  platform = builtins.getFlake (toString ./.);
  pkgs = import platform.inputs.nixpkgs {system = "x86_64-linux";};
  release = import ./website-release.nix {inherit pkgs webArchive cmsArchive;};
  # These three secret-free files are the entire test dependency, not the source
  # checkout. Hash before builtins.path so a mistaken private file never enters it.
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
  realm = checked "identity/tools/production-realm.ts" "c8cc120bdcfa56774aa5efc876864c2e7903d09b9fe81791f8287992bf2e0ee1";
  clients = checked "identity/tools/realm-config.ts" "68cd6d4b6286afc554a5c92dda5e3334a14b294d04fc19b6991383418724c839";
  input = checked "cms/src/auth/production-input.ts" "9600bd0aa47e28c2568afcc02cea47c9de13377301d64d6079f9af5c771c6ed9";
  contract = pkgs.runCommand "terrace-gateway-test-contract" {} ''
    mkdir -p $out/identity/tools $out/cms/src/auth
    cp ${realm} $out/identity/tools/production-realm.ts
    cp ${clients} $out/identity/tools/realm-config.ts
    cp ${input} $out/cms/src/auth/production-input.ts
    cp ${./website-gateway/fixture.ts} $out/fixture.ts
  '';
in
  pkgs.testers.runNixOSTest (import ./website-gateway-test.nix {inherit pkgs release contract;})
