{
  inputs = {
    disko.url = "github:nix-community/disko";
    git-hooks.url = "github:cachix/git-hooks.nix";
    nixos-anywhere.url = "github:nix-community/nixos-anywhere";
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    sops-nix.url = "github:Mic92/sops-nix";
    srvos.url = "github:nix-community/srvos";
    systems.url = "github:nix-systems/default";
    terranix.url = "github:terranix/terranix";
    opentofu-registry.url = "github:opentofu/registry";
    opentofu-registry.flake = false;

    canivete.url = "github:schradert/canivete";
  };
  outputs = inputs:
    inputs.canivete.lib.mkFlake {inherit inputs;} [./nix] {
      canivete.meta = {
        domain = "terracefclub.org";
        root = "servery";
        people.users.tristan.profiles.default.email = "tristan.schrader@terracefclub.org";
      };
      perSystem = {pkgs, ...}: {
        canivete.devShells.shells.default.packages = [pkgs.hcloud];
      };
    };
}
