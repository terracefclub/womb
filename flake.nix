{
  inputs = {
    disko.url = github:nix-community/disko;
    git-hooks.url = github:cachix/git-hooks.nix;
    nixos-anywhere.url = github:nix-community/nixos-anywhere;
    nixos-generators.url = github:nix-community/nixos-generators;
    nixpkgs.url = github:nixos/nixpkgs/nixos-unstable;
    sops-nix.url = github:Mic92/sops-nix;
    systems.url = github:nix-systems/default;
    terranix.url = github:terranix/terranix;

    canivete.url = github:schradert/canivete;
  };
  outputs = inputs:
    inputs.canivete.lib.mkFlake {inherit inputs;} [] {
      canivete.deploy.nixos.nodes.main.profiles.system.module = {config, ...}: {
        security.acme = {
          defaults.acceptTerms = true;
          defaults.email = "tristan.schrader@terracefclub.org";
          certs."vaultwarden.terracefclub.org".group = "vaultwarden";
        };

        services.nginx = {
          enable = true;
          recommendedGzipSettings = true;
          recommendedOptimisation = true;
          recommendedProxySettings = true;
          recommendedTlsSettings = true;
          virtualHosts."vaultwarden.terracefclub.org" = {
            enableACME = true;
            forceSSL = true;
            locations."/" = {
              proxyPass = "http://127.0.0.1:8000";
              proxyWebsockets = true;
            };
          };
        };

        services.vaultwarden = {
          enable = true;
          config = {
            DOMAIN = "https://vaultwarden.terracefclub.org";
            SIGNUPS_ALLOWED = false;
            SIGNUPS_DOMAIN_WHITELIST = "terracefclub.org";
            EMAIL_CHANGE_ALLOWED = false;
            SMTP_HOST = "smtp.terracefclub.org";
            SMTP_FROM = "vaultwarden@terracefclub.org";

            # TODO mobile push notifications
            # NOTE https://github.com/dani-garcia/vaultwarden/wiki/Enabling-Mobile-Client-push-notification
            # PUSH_ENABLED = true;
            # PUSH_INSTALLATION_ID = "";
            # PUSH_INSTALLATION_KEY = "";
          };
          # FIXME create environment file with sops-nix containing:
          # SMTP_USERNAME = (import /etc/nixos/secret/bitwarden.nix).SMTP_USERNAME;
          # SMTP_PASSWORD = (import /etc/nixos/secret/bitwarden.nix).SMTP_PASSWORD;
          # ADMIN_TOKEN = (import /etc/nixos/secret/bitwarden.nix).ADMIN_TOKEN;
          # environmentFile = "/etc/nixos/secret/bitwarden.env";
        };
      };
      perSystem.canivete.opentofu.workspaces.deploy = {
        plugins = ["hetznercloud/hcloud"];
        modules.main.resource = {
          hcloud_ssh_key.hcloud = {
            for_each = [
              {
                key = "";
                value = "";
              }
            ];
            name = "each.key";
            public_key = "each.value";
          };
          hcloud_server.main = {
            depends_on = ["hcloud_ssh_key.hcloud"];
            image = "debian-11";
            keep_disk = true;
            name = "main";
            server_type = "cpx21";
            ssh_keys = [""];
            backups = false;
            labels = {};
            location = "hel1";

            lifecycle.ignore_changes = ["ssh_keys"];
            prevent_destroy = true;
          };
        };
      };
    };
}
