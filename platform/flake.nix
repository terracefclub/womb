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

    checks.${system} = {
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
    };
  };
}
