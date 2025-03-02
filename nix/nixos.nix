{config, ...}: let
  inherit (config.canivete.meta) domain people root;
in {
  # FIXME build this on remote root
  canivete.deploy.nixos.nodes.${root}.profiles.system.module = {
    security.acme = {
      defaults.acceptTerms = true;
      defaults.email = people.users.tristan.profiles.default.email;
      certs."vaultwarden.${domain}".group = "vaultwarden";
    };

    services.nginx = {
      enable = true;
      recommendedGzipSettings = true;
      recommendedOptimisation = true;
      recommendedProxySettings = true;
      recommendedTlsSettings = true;
      virtualHosts."vaultwarden.${domain}" = {
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
