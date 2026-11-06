{packagedOnly ? false}: let
  platform = builtins.getFlake (toString ./.);
  pkgs = import platform.inputs.nixpkgs {system = "x86_64-linux";};
  host = import "${platform.inputs.nixpkgs}/nixos/lib/eval-config.nix" {
    system = "x86_64-linux";
    modules = [
      ./backup.nix
      {
        system.stateVersion = "26.11";
        services.postgresql.enable = true;
        terrace.backup = {
          enable = true;
          postgresql.databases = ["saved"];
          targets.proof = {
            enable = true;
            repository = "/unused-synthetic-proof";
            passwordFile = "/unused-synthetic-proof-password";
          };
        };
      }
    ];
  };
  installed = name: pkgs.lib.findFirst (package: (package.name or "") == name) null host.config.environment.systemPackages;
  backup = installed "terrace-backup";
  restore = installed "terrace-backup-restore";
in
  pkgs.runCommand (
    if packagedOnly
    then "terrace-backup-packaged-helper-proof"
    else "terrace-postgresql-bootstrap-proof"
  ) {
    nativeBuildInputs = [pkgs.postgresql_18 pkgs.python3];
  } ''
    python -B ${./backup/test-bootstrap-native.py} ${./backup/bootstrap-identity.py} "$out" \
      ${backup}/bin/terrace-backup ${restore}/bin/terrace-backup-restore ${pkgs.lib.optionalString packagedOnly "--packaged-only"}
  ''
