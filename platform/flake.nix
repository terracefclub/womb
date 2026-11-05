{
  description = "Terrace single-node platform foundation";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = {
    self,
    nixpkgs,
  }: let
    system = "x86_64-linux";
    pkgs = import nixpkgs {inherit system;};
  in {
    nixosModules.single-node = import ./single-node.nix;
    nixosModules.hetzner = import ./hetzner.nix;
    nixosModules.relay = import ./relay.nix;
    nixosModules.backup = ./backup.nix;
    nixosModules.demos = import ./demos.nix;
    nixosModules.identity = import ./identity.nix;
    nixosModules.website = import ./website.nix;
    nixosModules.windmill = import ./windmill.nix;
    nixosModules.website-gateway = import ./website-gateway.nix;
    nixosModules.documenso = import ./documenso.nix;

    checks.${system} = {
      documenso = pkgs.testers.runNixOSTest (import ./documenso-test.nix {
        inherit pkgs;
        module = self.nixosModules.documenso;
      });
      single-node = pkgs.testers.runNixOSTest (import ./test.nix {
        inherit pkgs;
        module = self.nixosModules.single-node;
      });
      relay = pkgs.testers.runNixOSTest (import ./relay-test.nix {
        inherit pkgs;
        module = self.nixosModules.relay;
      });
      backup = pkgs.testers.runNixOSTest (import ./backup-test.nix {
        inherit pkgs;
        module = self.nixosModules.backup;
        relayModule = self.nixosModules.relay;
      });
      demos = pkgs.testers.runNixOSTest (import ./demos-test.nix {
        inherit pkgs;
        module = self.nixosModules.demos;
      });
      identity = pkgs.testers.runNixOSTest (import ./identity-test.nix {
        inherit pkgs;
        module = self.nixosModules.identity;
      });
      identity-restore = pkgs.testers.runNixOSTest (import ./identity-restore-test.nix {
        inherit pkgs;
        identityModule = self.nixosModules.identity;
        backupModule = self.nixosModules.backup;
      });
    };
  };
}
