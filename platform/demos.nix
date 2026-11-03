{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.terrace.demos;

  # Everything here is loopback only and denied any other IP traffic by systemd.
  ports = {
    homeAssistant = 8123;
    frigateUnauthenticated = 5000; # Frigate's internal API, loopback by upstream design
    frigateApi = 5001;
    frigateWeb = 80; # nginx vhost with Frigate authentication
    go2rtcApi = 1984;
    go2rtcRtsp = 8554;
    go2rtcWebrtc = 8555;
  };
  loopback = "127.0.0.1";
  camera = "synthetic_black";
  hassDir = "/var/lib/terrace-demo-hass";
  frigateDir = "/var/lib/terrace-demo-frigate";
  frigateCache = "/var/cache/terrace-demo-frigate";
  frigateMounts = ["${frigateDir}:/var/lib/frigate" "${frigateCache}:/var/cache/frigate"];

  # Bounded demo resources; see docs/demos.md for measurements. Hosts may override them.
  limits = {
    homeAssistant = {
      MemoryHigh = "448M";
      MemoryMax = "640M";
      CPUQuota = "100%";
      TasksMax = 96;
    };
    frigate = {
      MemoryHigh = "1152M";
      MemoryMax = "1536M";
      CPUQuota = "100%";
      TasksMax = 320;
    };
    go2rtc = {
      MemoryHigh = "64M";
      MemoryMax = "96M";
      CPUQuota = "50%";
      TasksMax = 64;
    };
    nginx = {
      MemoryHigh = "80M";
      MemoryMax = "128M";
      CPUQuota = "50%";
      TasksMax = 128;
    };
  };

  # No network beyond this host, memory swapped out, or unbounded growth, whatever the application attempts.
  bound = limit:
    {
      MemorySwapMax = 0;
      IPAddressDeny = "any";
      IPAddressAllow = "localhost";
    }
    // lib.mapAttrs (_: lib.mkDefault) limit;

  # Plausible hourly-scale curves from the clock alone: no devices, files or network are read.
  temperature = {
    name,
    base,
    amplitude,
    periodSeconds,
    phase,
  }: {
    name = "Synthetic ${name} Temperature";
    unique_id = "synthetic_${lib.toLower name}_temperature";
    unit_of_measurement = "°C";
    device_class = "temperature";
    state_class = "measurement";
    state = "{{ (${toString base} + ${toString amplitude} * sin(now().timestamp() / ${toString periodSeconds} + ${toString phase})) | round(1) }}";
    attributes.synthetic = true;
  };
  occupancy = {
    name,
    offsetMinutes,
  }: {
    name = "Synthetic ${name} Occupancy";
    unique_id = "synthetic_${lib.toLower name}_occupancy";
    device_class = "occupancy";
    state = "{{ ((now().timestamp() / 60) | int + ${toString offsetMinutes}) % 12 < 5 }}";
    attributes.synthetic = true;
  };

  syntheticTemplates = [
    {
      sensor = [
        (temperature {
          name = "Kitchen";
          base = 22;
          amplitude = 2;
          periodSeconds = 900;
          phase = 0;
        })
        (temperature {
          name = "Pool";
          base = 27;
          amplitude = 1;
          periodSeconds = 1800;
          phase = 1;
        })
        (temperature {
          name = "Solarium";
          base = 25;
          amplitude = 6;
          periodSeconds = 1200;
          phase = 2;
        })
      ];
      binary_sensor = [
        (occupancy {
          name = "Kitchen";
          offsetMinutes = 0;
        })
        (occupancy {
          name = "Pool";
          offsetMinutes = 4;
        })
        (occupancy {
          name = "Solarium";
          offsetMinutes = 8;
        })
      ];
    }
  ];

  demoCtl = pkgs.writeShellApplication {
    name = "terrace-demo";
    runtimeInputs = [pkgs.coreutils pkgs.curl pkgs.findutils pkgs.jq pkgs.systemd];
    text = ''
      units=(${lib.optionalString cfg.homeAssistant.enable "home-assistant"} ${lib.optionalString cfg.frigate.enable "go2rtc frigate nginx"})
      stateDirs=(${lib.optionalString cfg.homeAssistant.enable (lib.escapeShellArg hassDir)} ${lib.optionalString cfg.frigate.enable "${frigateDir} ${frigateCache}"})
      usage() {
        echo "usage: terrace-demo status | teardown --yes" >&2
        exit 2
      }
      status() {
        failed=0
        for unit in "''${units[@]}"; do
          state=$(systemctl is-active "$unit.service") || failed=1
          printf '%-15s %s memory=%s/%s cpu_ns=%s\n' "$unit" \
            "$state" \
            "$(systemctl show -p MemoryCurrent --value "$unit.service")" \
            "$(systemctl show -p MemoryMax --value "$unit.service")" \
            "$(systemctl show -p CPUUsageNSec --value "$unit.service")"
        done
        probe() { # name url jq-filter
          if curl -fsS --max-time 5 "$2" | jq -e "$3" >/dev/null 2>&1; then
            echo "ok   $1"
          else
            echo "FAIL $1"
            failed=1
          fi
        }
        ${lib.optionalString cfg.homeAssistant.enable ''
        probe "home assistant answers (onboarding API)" "http://${loopback}:${toString ports.homeAssistant}/api/onboarding" 'type == "array"'
      ''}
        ${lib.optionalString cfg.frigate.enable ''
        probe "go2rtc serves ${camera}" "http://${loopback}:${toString ports.go2rtcApi}/api/streams" 'has("${camera}")'
        probe "frigate camera ${camera} has frames" "http://${loopback}:${toString ports.frigateUnauthenticated}/api/stats" '.cameras["${camera}"].camera_fps > 0'
      ''}
        exit "$failed"
      }
      teardown() {
        [ "''${1:-}" = "--yes" ] || usage
        systemctl stop "''${units[@]/%/.service}"
        # Contents only: the unit directories belong to the units and are recreated on start.
        for dir in "''${stateDirs[@]}"; do
          find "$dir" -xdev -mindepth 1 -delete
        done
        echo "Selected synthetic state removed; start the demo units to begin from empty."
      }
      case "''${1:-}" in
        status) status ;;
        teardown) teardown "''${2:-}" ;;
        *) usage ;;
      esac
    '';
  };
in {
  options.terrace.demos = {
    enable = lib.mkEnableOption "synthetic Home Assistant and Frigate demonstrations (no real devices, cameras or cloud)";
    homeAssistant.enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Run Home Assistant with clearly labelled synthetic sensors.";
    };
    frigate.enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Run Frigate and go2rtc on a locally generated black video stream.";
    };
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [
    {
      environment.systemPackages = [demoCtl];
      assertions = [
        {
          assertion = cfg.homeAssistant.enable || cfg.frigate.enable;
          message = "Enable at least one Terrace demo service, or disable terrace.demos.";
        }
        {
          assertion = !cfg.frigate.enable || builtins.attrNames config.services.nginx.virtualHosts == ["localhost"];
          message = "The Frigate demo requires a dedicated host nginx service; its loopback-only network policy cannot serve other vhosts.";
        }
        {
          # Port 80 is the loopback vhost here but may legitimately be public for other services.
          assertion = !(lib.any (port: lib.elem port (lib.attrValues (removeAttrs ports ["frigateWeb"]))) config.networking.firewall.allowedTCPPorts);
          message = "Terrace synthetic demos must not be opened in the host firewall.";
        }
      ];
    }

    (lib.mkIf cfg.homeAssistant.enable {
      services.home-assistant = {
        enable = true;
        # Only the integrations configured below; nothing that discovers devices or calls a cloud.
        configDir = hassDir;
        extraComponents = [];
        config = {
          homeassistant = {
            name = "Terrace SYNTHETIC demo";
            unit_system = "metric";
            time_zone = "America/New_York";
            latitude = 0.0;
            longitude = 0.0;
            elevation = 0;
          };
          http = {
            server_host = [loopback];
            server_port = ports.homeAssistant;
          };
          logger.default = "warning";
          recorder = {
            purge_keep_days = 1;
            commit_interval = 30;
          };
          template = syntheticTemplates;
        };
      };
      # HA's built-in HTTP resolver binds mDNS sockets even without discovery.
      # Give it only lo, then pass a host-loopback listener into that namespace.
      systemd.services.terrace-demo-ha-network = {
        description = "Loopback-only network for the synthetic Home Assistant resolver";
        path = [pkgs.iproute2];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          PrivateNetwork = true;
          CapabilityBoundingSet = ["CAP_NET_ADMIN"];
        };
        script = ''
          test "$(ip -o link show | cut -d: -f2)" = " lo"
          ip link set dev lo multicast on
          ip route replace 224.0.0.0/4 dev lo
        '';
      };
      systemd.services.home-assistant = {
        after = ["terrace-demo-ha-network.service"];
        requires = ["terrace-demo-ha-network.service"];
        unitConfig.JoinsNamespaceOf = "terrace-demo-ha-network.service";
        wants = ["terrace-demo-ha.socket"];
        serviceConfig = bound limits.homeAssistant // {PrivateNetwork = true;};
      };
      systemd.sockets.terrace-demo-ha = {
        wantedBy = ["sockets.target"];
        partOf = ["home-assistant.service"];
        listenStreams = ["${loopback}:${toString ports.homeAssistant}"];
      };
      systemd.services.terrace-demo-ha = {
        requires = ["home-assistant.service"];
        after = ["home-assistant.service"];
        partOf = ["home-assistant.service"];
        unitConfig.JoinsNamespaceOf = "home-assistant.service";
        serviceConfig = {
          ExecStart = "${config.systemd.package}/lib/systemd/systemd-socket-proxyd --connections-max=32 ${loopback}:${toString ports.homeAssistant}";
          PrivateNetwork = true;
          DynamicUser = true;
          MemoryMax = "32M";
          TasksMax = 8;
        };
      };
    })

    (lib.mkIf cfg.frigate.enable {
      services.go2rtc = {
        enable = true;
        settings = {
          api.listen = "${loopback}:${toString ports.go2rtcApi}";
          rtsp.listen = "${loopback}:${toString ports.go2rtcRtsp}";
          webrtc.listen = "${loopback}:${toString ports.go2rtcWebrtc}";
          # go2rtc's built-in generator: a black 320x240 5 fps H.264 stream, encoded only while a consumer is attached.
          streams.${camera} = "ffmpeg:virtual?video=color&color=black&size=320x240&rate=5#video=h264";
        };
      };
      # Frigate and nginx expect fixed paths. Map them to demo-owned directories
      # inside both units so startup and teardown leave ordinary service data alone.
      systemd.tmpfiles.rules = [
        "d ${frigateDir} 0750 frigate frigate -"
        "d ${frigateCache} 0750 frigate frigate -"
      ];
      services.frigate = {
        enable = true;
        hostname = "localhost";
        settings = {
          mqtt.enabled = false;
          telemetry.version_check = false;
          detectors.cpu = {
            type = "cpu";
            num_threads = 1;
          };
          detect = {
            enabled = false;
            width = 320;
            height = 240;
            fps = 5;
          };
          record.enabled = false;
          snapshots.enabled = false;
          birdseye.enabled = false;
          audio.enabled = false;
          notifications.enabled = false;
          cameras.${camera}.ffmpeg.inputs = [
            {
              path = "rtsp://${loopback}:${toString ports.go2rtcRtsp}/${camera}";
              roles = ["detect"];
            }
          ];
        };
      };
      # The NixOS Frigate module also adds an RTMP listener on every interface; Frigate 0.18 restreams through go2rtc instead.
      services.nginx.appendConfig = lib.mkForce "";
      services.nginx.virtualHosts.localhost.listen = [
        {
          addr = loopback;
          port = ports.frigateWeb;
        }
      ];
      systemd.services = {
        go2rtc.serviceConfig =
          bound limits.go2rtc
          // {
            NoNewPrivileges = true;
            PrivateTmp = true;
            ProtectHome = true;
            ProtectSystem = "strict";
            CapabilityBoundingSet = "";
          };
        frigate.serviceConfig =
          bound limits.frigate
          // {
            # Upstream logs the random first-run administrator password at info level.
            LogFilterPatterns = ["~.*Password:.*"];
            StateDirectory = lib.mkForce "terrace-demo-frigate";
            CacheDirectory = lib.mkForce ["terrace-demo-frigate" "terrace-demo-frigate/model_cache"];
            BindPaths = frigateMounts;
            # Bounded shared memory for decoded frames instead of the default half of RAM.
            TemporaryFileSystem = lib.mkForce "/dev/shm:mode=1777,nosuid,nodev,size=128m";
          };
        # Frigate's module owns this nginx service; the assertion above prevents accidental sharing.
        nginx.serviceConfig = bound limits.nginx // {BindReadOnlyPaths = frigateMounts;};
      };
    })
  ]);
}
