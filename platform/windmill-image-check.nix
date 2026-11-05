# Candidate arguments permit fixed-output hash preflight without changing the
# runtime pin. This only fetches the official image archive; it starts no app.
{
  version ? null,
  imageDigest ? null,
  archiveHash ? null,
}: let
  platform = builtins.getFlake (toString ./.);
  pkgs = import platform.inputs.nixpkgs {system = "x86_64-linux";};
  current = builtins.fromJSON (builtins.readFile ./windmill/release.json);
  pin = current // pkgs.lib.filterAttrs (_: value: value != null) {inherit version imageDigest archiveHash;};
in
  import ./windmill/image.nix {inherit pkgs pin;}
