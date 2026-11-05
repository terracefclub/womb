let
  platform = builtins.getFlake (toString ./.);
  pkgs = import platform.inputs.nixpkgs {system = "x86_64-linux";};
in
  pkgs.runCommand "terrace-postgresql-bootstrap-proof" {
    nativeBuildInputs = [pkgs.postgresql_18 pkgs.python3];
  } ''
    python -B ${./backup/test-bootstrap-native.py} ${./backup/bootstrap-identity.py} "$out"
  ''
