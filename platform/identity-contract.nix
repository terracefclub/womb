{
  pkgs,
  tfcSource,
}: let
  # The reviewed, secret-free TFC realm producer: exactly the files pinned in
  # identity-contract.json (shared with test-identity-contract.nix). Nothing
  # else from the checkout, and no secret, enters the store.
  pins = builtins.fromJSON (builtins.readFile ./identity-contract.json);
  checked = relative: let
    path = tfcSource + "/" + relative;
  in
    if builtins.hashFile "sha256" path != pins.${relative}
    then throw "The identity contract requires the reviewed TFC producer sources (identity-contract.json)."
    else
      builtins.path {
        inherit path;
        name = builtins.baseNameOf relative;
      };
in
  pkgs.runCommand "terrace-identity-contract" {} (pkgs.lib.concatStrings (pkgs.lib.mapAttrsToList (relative: _: ''
      mkdir -p $out/${builtins.dirOf relative}
      cp ${checked relative} $out/${relative}
    '')
    pins))
