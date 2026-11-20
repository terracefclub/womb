# Keycloak production administration for the app role (docs/identity-admin.md): the
# root-only operator tool, the unit that runs Keycloak's own bootstrap-admin recovery
# command with Keycloak stopped, and the invariants that keep administration off the
# public proxy. Imported by app.nix only.
{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.terrace.production;
  app = cfg.app;
  production = cfg.instance == "production";
  keycloak = config.systemd.services.keycloak;
  tool = pkgs.callPackage ./identity-admin-package.nix {postgresql = config.services.postgresql.package;};
  wrapper = config.security.wrappers.terrace-identity-admin;
  backend = "http://127.0.0.1:8088";
  # The upstream unit's last line; the bootstrap unit runs everything before it unchanged.
  upstreamStart = "kc.sh --verbose start --optimized --import-realm";
  bootstrapStart = ''
    TERRACE_BOOTSTRAP_PASSWORD="$(<"$CREDENTIALS_DIRECTORY/bootstrap-password")"
    export TERRACE_BOOTSTRAP_PASSWORD
    kc.sh bootstrap-admin user --optimized --username "$(<"$CREDENTIALS_DIRECTORY/bootstrap-username")" \
      --password:env TERRACE_BOOTSTRAP_PASSWORD --no-prompt
  '';
  # Exactly what the identity module proxies; everything else on that host name answers 404.
  proxied = ["/realms/terrace/" "/resources/" "= /.well-known/oauth-authorization-server/realms/terrace"];
  denied = ["/" "/realms/terrace/clients-registrations"];
  identityHost = config.services.nginx.virtualHosts.${app.identityHostname} or {locations = {};};
  # Keycloak's backend and management ports, whatever host spelling reaches them (127.1, [::ffff:127.0.0.1], 0, a name).
  mentionsBackend = text: lib.any (line: builtins.match ".*:(8088|9000)([^0-9].*)?" line != null) (lib.splitString "\n" (toString text));
  # Free-form nginx configuration that could carry a proxy_pass of its own.
  globalNginx = with config.services.nginx; [commonHttpConfig appendHttpConfig appendConfig streamConfig];
  # Every place nginx could forward to Keycloak's backend or management port.
  backendRoutes = lib.concatLists (lib.mapAttrsToList (host: vhost:
    lib.concatLists (lib.mapAttrsToList (path: location:
      lib.optional (mentionsBackend (location.proxyPass or "") || mentionsBackend (location.extraConfig or "")) "${host} ${path}")
    vhost.locations)
    ++ lib.optional (mentionsBackend (vhost.extraConfig or "")) "${host} (server block)")
  config.services.nginx.virtualHosts);
  allowedRoutes = map (path: "${app.identityHostname} ${path}") proxied;
  upstreamRoutes =
    lib.filter (name: lib.any mentionsBackend (lib.attrNames config.services.nginx.upstreams.${name}.servers ++ [config.services.nginx.upstreams.${name}.extraConfig])) (lib.attrNames config.services.nginx.upstreams)
    ++ lib.optional (lib.any mentionsBackend globalNginx) "(global nginx configuration)";
  bootstrapUnit = config.systemd.services.terrace-identity-bootstrap-admin;
  # Runs as root (systemd's `+`) after the bootstrap command, whatever happened to it or to the tool.
  # It also records this run's outcome next to the tool's nonce: a oneshot unit may be unloaded once inactive,
  # so the tool cannot read the result from the unit afterwards.
  afterBootstrap = pkgs.writeShellScript "terrace-identity-after-bootstrap" ''
    d=/run/terrace-identity-admin/kc-bootstrap
    ${pkgs.coreutils}/bin/rm -f "$d/username" "$d/password"
    if [ -f "$d/nonce" ]; then
      printf '%s %s\n' "$(${pkgs.coreutils}/bin/cat "$d/nonce")" "''${SERVICE_RESULT:-unknown}" >"$d/result.new"
      ${pkgs.coreutils}/bin/mv "$d/result.new" "$d/result"
      ${pkgs.coreutils}/bin/rm -f "$d/nonce"
    fi
    ${pkgs.systemd}/bin/systemctl reset-failed keycloak.service || true
    exec ${pkgs.systemd}/bin/systemctl start --no-block keycloak.service
  '';
  clients = {
    terrace-web = [
      {
        unit = "terrace-web.service";
        variable = "MEMBER_AUTH_CLIENT_SECRET";
      }
    ];
    terrace-community = [
      {
        unit = "terrace-cms.service";
        variable = "MEMBER_AUTH_INTROSPECTION_SECRET";
      }
    ];
    terrace-cms = [
      {
        unit = "terrace-cms.service";
        variable = "CMS_AUTH_CLIENT_SECRET";
      }
    ];
    terrace-editorial = [
      {
        unit = "terrace-cms.service";
        variable = "CMS_AUTH_INTROSPECTION_SECRET";
      }
    ];
  };
in {
  options.terrace.production.identityAdmin.temporaryMaxAgeMinutes = lib.mkOption {
    type = lib.types.ints.unsigned;
    default = 240;
    description = "A temporary bootstrap or recovery Keycloak administrator older than this fails terrace-identity-temporary-admin-check (logged at crit).";
  };
  config = {
    assertions = [
      {
        assertion = lib.hasInfix upstreamStart keycloak.script;
        message = "Terrace identity administration: the upstream Keycloak unit no longer ends with `${upstreamStart}`; review the bootstrap unit.";
      }
      {
        assertion =
          wrapper.owner
          == "root"
          && wrapper.group == "root"
          && wrapper.permissions == "u+rx,g-rwx,o-rwx"
          && !wrapper.setuid
          && !wrapper.setgid
          && wrapper.capabilities == ""
          && !(lib.any (package: lib.getName package == "terrace-identity-admin") config.environment.systemPackages);
        message = "Terrace identity administration: the tool is installed only as a root-only wrapper (0500, no setuid/setgid/capabilities) and not on the general PATH.";
      }
      {
        assertion =
          lib.sort lib.lessThan (lib.attrNames identityHost.locations)
          == lib.sort lib.lessThan (proxied ++ denied)
          && lib.all (path: toString (identityHost.locations.${path}.return or "") == "404" && identityHost.locations.${path}.proxyPass == null) denied
          && lib.all (path: identityHost.locations.${path}.proxyPass == backend) proxied
          && lib.sort lib.lessThan backendRoutes == lib.sort lib.lessThan allowedRoutes
          && upstreamRoutes == []
          && lib.all (path: !(lib.hasInfix "_pass" (identityHost.locations.${path}.extraConfig or ""))) (lib.attrNames identityHost.locations)
          && !(lib.hasInfix "location" (identityHost.extraConfig or ""));
        message = "Terrace identity administration stays off the public proxy: only the Terrace realm, resources and discovery reach Keycloak (routes: ${toString backendRoutes}${toString upstreamRoutes}).";
      }
      {
        assertion =
          !(config.services.keycloak.settings ? hostname-admin)
          && !(lib.any (name: lib.hasPrefix "KC_BOOTSTRAP_ADMIN" name) (lib.attrNames keycloak.environment))
          && (!production || !(config.sops.secrets ? "identity/bootstrap-admin-password") && !(lib.any (name: lib.hasInfix "bootstrap" name) (lib.attrNames config.sops.templates)));
        message = "Terrace production declares no bootstrap administrator credential and no admin console hostname: the escrowed bootstrap password is handed over once at run time (docs/identity-admin.md).";
      }
      {
        assertion = bootstrapUnit.wantedBy == [] && builtins.elem "keycloak.service" bootstrapUnit.after && (bootstrapUnit.requiredBy or []) == [] && (bootstrapUnit.upheldBy or []) == [] && builtins.elem "keycloak.service" bootstrapUnit.conflicts && bootstrapUnit.serviceConfig.ExecStopPost == "+${afterBootstrap}";
        message = "Terrace identity administration: the bootstrap unit is never started at boot, conflicts with Keycloak and always starts Keycloak again afterwards.";
      }
    ];

    # The wrapper adds no privilege (not setuid, no capabilities). It is the tool's only entry point:
    # mode 0500, so no other account can run it, and the package stays off the general PATH.
    security.wrappers.terrace-identity-admin = {
      source = "${tool}/bin/terrace-identity-admin";
      owner = "root";
      group = "root";
      permissions = "u+rx,g-rwx,o-rwx";
      setuid = false;
      setgid = false;
    };

    environment.etc."terrace/identity-admin.json" = {
      mode = "0400";
      text = builtins.toJSON {
        base = backend;
        management = "http://127.0.0.1:9000";
        realm = "terrace";
        # The pinned release's realm template: sops placeholders, never secret values.
        definition = "${config.sops.templates."identity/terrace-realm.json".file}";
        policy = "${../identity/realm-policy.jq}";
        bootstrapUnit = "terrace-identity-bootstrap-admin.service";
        inherit (config.terrace.production.identityAdmin) temporaryMaxAgeMinutes;
        clientSecrets =
          lib.mapAttrs (client: consumers: {
            file = config.terrace.production.secrets.path."identity/client/${client}";
            inherit consumers;
          })
          clients;
      };
    };

    # Keycloak's recovery command, `kc.sh bootstrap-admin user`, run with exactly the upstream
    # unit's preparation (configuration, database password, providers) while Keycloak is
    # stopped, as upstream requires. Started only by terrace-identity-admin; never at boot.
    # A forgotten bootstrap or recovery administrator becomes a failed unit and a crit journal line.
    terrace.production.writerTimers = ["terrace-identity-temporary-admin-check"];
    systemd.services.terrace-identity-temporary-admin-check = {
      description = "Fail while a temporary Keycloak administrator is older than allowed";
      after = ["keycloak.service"];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${tool}/bin/terrace-identity-admin check-temporary";
        LimitCORE = 0;
      };
    };
    systemd.timers.terrace-identity-temporary-admin-check.timerConfig = {
      OnBootSec = "10min";
      OnUnitActiveSec = "15min";
    };

    systemd.services.terrace-identity-bootstrap-admin = {
      description = "Create a temporary Keycloak administrator (terrace-identity-admin only)";
      # Conflicts= alone sets no order: start only once Keycloak (sharing /run/keycloak) has fully stopped.
      after = keycloak.after ++ ["keycloak.service"];
      requires = keycloak.bindsTo;
      conflicts = ["keycloak.service"];
      path = keycloak.path;
      environment = {
        inherit (keycloak.environment) KC_HOME_DIR KC_CONF_DIR JAVA_OPTS_APPEND;
      };
      script = builtins.replaceStrings [upstreamStart] [bootstrapStart] keycloak.script;
      serviceConfig = {
        Type = "oneshot";
        inherit (keycloak.serviceConfig) User Group DynamicUser RuntimeDirectory RuntimeDirectoryMode;
        LoadCredential =
          keycloak.serviceConfig.LoadCredential
          ++ [
            "bootstrap-username:/run/terrace-identity-admin/kc-bootstrap/username"
            "bootstrap-password:/run/terrace-identity-admin/kc-bootstrap/password"
          ];
        # Like Keycloak's own unit: startup output is not logged.
        # Removes the credential files and starts Keycloak again even if the tool was killed.
        ExecStopPost = "+${afterBootstrap}";
        StandardOutput = lib.mkDefault "null";
        StandardError = lib.mkDefault "null";
        UMask = "0077";
        TimeoutStartSec = 300;
        MemoryMax = "2G";
        LimitCORE = 0;
      };
    };
  };
}
