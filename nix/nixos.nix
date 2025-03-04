{
  config,
  inputs,
  lib,
  ...
}: let
  inherit (config.canivete.meta) domain people root;
in {
  # FIXME build this on remote root
  canivete.deploy.nixos.nodes.${root}.profiles.system.module = {
    imports = [
      inputs.disko.nixosModules.disko
      inputs.srvos.nixosModules.hardware-hetzner-cloud
      inputs.srvos.nixosModules.mixins-nginx
      inputs.srvos.nixosModules.server
    ];

    disko.devices.disk.base = {
      device = "/dev/sda";
      type = "disk";
      content.type = "gpt";
      content.partitions = {
        boot = {
          priority = 1;
          type = "EF02";
          size = "1M";
        };
        ESP = {
          priority = 2;
          type = "EF00";
          size = "512M";
          content.type = "filesystem";
          content.format = "vfat";
          content.mountpoint = "/boot";
        };
        root = {
          priority = 3;
          size = "100%";
          content.type = "filesystem";
          content.format = "ext4";
          content.mountpoint = "/";
        };
      };
    };

    system.stateVersion = "25.05";
    users.users.root.openssh.authorizedKeys.keys = [(lib.fileContents ./ssh/tristan.pub)];

    security.acme = {
      acceptTerms = true;
      defaults.email = people.users.tristan.profiles.default.email;
      defaults.server = "https://acme-staging-v02.api.letsencrypt.org/directory";
      # certs."vaultwarden.${domain}".group = "vaultwarden";
    };

    services.nginx.virtualHosts."vaultwarden.${domain}" = {
      enableACME = true;
      forceSSL = true;
      locations."/" = {
        proxyPass = "http://127.0.0.1:8000";
        proxyWebsockets = true;
      };
    };

    services.vaultwarden = {
      enable = true;
      config = {
        DOMAIN = "https://vaultwarden.${domain}";
        SIGNUPS_ALLOWED = false;
        SIGNUPS_DOMAIN_WHITELIST = domain;
        EMAIL_CHANGE_ALLOWED = false;
        SMTP_HOST = "smtp.${domain}";
        SMTP_FROM = "vaultwarden@${domain}";

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
}
