{
  inputs = {
    deploy-rs.url = "github:serokell/deploy-rs";
    devenv.url = "github:cachix/devenv";
    disko.url = "github:nix-community/disko";
    nixos-anywhere.url = "github:nix-community/nixos-anywhere";
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    sops-nix.url = "github:Mic92/sops-nix";
    srvos.url = "github:nix-community/srvos";
    treefmt-nix.url = "github:numtide/treefmt-nix";

    canivete.url = "github:schradert/canivete";
  };
  outputs = inputs:
    inputs.canivete.lib.mkFlake {inherit inputs;} {
      imports = [./devenv ./nixos];
      canivete.meta = {
        domain = "terracefclub.org";
        people.me = "tristan";
        people.users.tristan = {
          name = "Tristan Schrader";
          profiles.default = {
            email = "tristan.schrader@terracefclub.org";
            sshPubKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIC0NBh1tPsY2hs3qW8BEZ2wn+eaOL33blaaQBKI27YZu";
          };
        };
      };
    };
}
