# Public installer image. The pinned runner transfers the SSH files at runtime.
let
  nixpkgs = (builtins.getFlake "path:${toString ./.}").inputs.nixpkgs;
  images = builtins.getFlake "github:nix-community/nixos-images/aa38dbbdf0e955baef7e03dfc4265ae3fdac4808";
  installer = nixpkgs.lib.nixosSystem {
    system = "x86_64-linux";
    modules = [
      images.nixosModules.kexec-installer
      ({lib, ...}: {
        networking.hostName = "terrace-ram-installer";
        networking.firewall.enable = lib.mkForce true;
        networking.firewall.allowedTCPPorts = [22];
        networking.firewall.allowedUDPPorts = lib.mkForce [];
        environment.etc."terrace-ram-installer".text = "terrace-ram-installer-v1";
        services.openssh = {
          enable = true;
          authorizedKeysFiles = lib.mkForce ["/root/.ssh/authorized_keys"];
          hostKeys = [
            {
              path = "/etc/ssh/ssh_host_ed25519_key";
              type = "ed25519";
            }
          ];
          settings = {
            AllowUsers = ["root"];
            PasswordAuthentication = false;
            KbdInteractiveAuthentication = false;
            PermitRootLogin = "prohibit-password";
          };
        };
      })
    ];
  };
in
  installer.config.system.build.kexecInstallerTarball
