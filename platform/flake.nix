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

    checks.${system}.single-node = pkgs.testers.runNixOSTest (import ./test.nix {
      inherit pkgs;
      module = self.nixosModules.single-node;
    });
  };
}
