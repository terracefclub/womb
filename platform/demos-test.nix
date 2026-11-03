{
  pkgs,
  module,
}: let
  haTest = pkgs.writeShellScriptBin "demos-ha-test" ''
    exec ${(pkgs.python3.withPackages (ps: [ps.aiohttp]))}/bin/python3 ${./demos-ha-test.py} "$@"
  '';
in {
  name = "terrace-synthetic-demos";
  nodes = {
    server = {lib, ...}: {
      imports = [module];
      terrace.demos.enable = true;
      systemd.tmpfiles.rules = map (dir: "f ${dir}/unrelated-marker 0600 root root - retained") [
        "/var/lib/hass"
        "/var/lib/frigate"
        "/var/cache/frigate"
      ];
      environment.systemPackages = [pkgs.curl pkgs.jq haTest];
      virtualisation.memorySize = 4096;
      virtualisation.cores = 2;
      specialisation.demos-off.configuration.terrace.demos.enable = lib.mkForce false;
      specialisation.ha-only.configuration.terrace.demos.frigate.enable = lib.mkForce false;
      specialisation.frigate-only.configuration.terrace.demos.homeAssistant.enable = lib.mkForce false;
    };
    client = {...}: {
      environment.systemPackages = [pkgs.curl pkgs.netcat-openbsd pkgs.python3];
      networking.firewall.allowedTCPPorts = [8000];
    };
  };
  testScript = ''
    import json
    import re

    HA = "http://127.0.0.1:8123"
    FRIGATE = "http://127.0.0.1:5000"
    GO2RTC = "http://127.0.0.1:1984"
    CAMERA = "synthetic_black"
    UNITS = ["home-assistant", "frigate", "go2rtc", "nginx"]
    TEMPERATURES = {
        "sensor.synthetic_kitchen_temperature": (19, 25),
        "sensor.synthetic_pool_temperature": (25, 29),
        "sensor.synthetic_solarium_temperature": (18, 32),
    }
    OCCUPANCY = [
        "binary_sensor.synthetic_kitchen_occupancy",
        "binary_sensor.synthetic_pool_occupancy",
        "binary_sensor.synthetic_solarium_occupancy",
    ]

    def js(command):
        return json.loads(server.succeed(command))

    def wait_for_demos():
        for unit in ["go2rtc", "frigate", "home-assistant", "nginx"]:
            server.wait_for_unit(unit + ".service")
        server.wait_until_succeeds(f"curl -sf --max-time 5 {HA}/api/onboarding", timeout=300)
        server.wait_until_succeeds(f"curl -sf --max-time 5 {FRIGATE}/api/version", timeout=300)

    def frigate_camera():
        return js(f"curl -sf --max-time 5 {FRIGATE}/api/stats")["cameras"][CAMERA]

    def wait_for_camera():
        server.wait_until_succeeds(
            f"curl -sf --max-time 5 {FRIGATE}/api/stats | jq -e '.cameras.{CAMERA}.camera_fps > 0'",
            timeout=300,
        )

    def ha_states():
        return {
            state["entity_id"]: state
            for state in js("demos-ha-test states")
        }

    def wait_for_sensors():
        expected = list(TEMPERATURES) + OCCUPANCY
        server.wait_until_succeeds(
            "demos-ha-test states"
            + " | jq -e '[.[] | select(.entity_id | startswith(\"sensor.synthetic_\") or startswith(\"binary_sensor.synthetic_\"))"
            + " | select(.state != \"unknown\" and .state != \"unavailable\")] | length == " + str(len(expected)) + "'",
            timeout=180,
        )

    def check_sensors():
        states = ha_states()
        for entity, (low, high) in TEMPERATURES.items():
            state = states[entity]
            value = float(state["state"])
            assert low <= value <= high, (entity, value)
            assert state["attributes"]["unit_of_measurement"] == "°C", state
            assert state["attributes"]["device_class"] == "temperature", state
        for entity in OCCUPANCY:
            assert states[entity]["state"] in ("on", "off"), states[entity]
        for entity in list(TEMPERATURES) + OCCUPANCY:
            attributes = states[entity]["attributes"]
            assert attributes["synthetic"] in (True, "True", "true"), attributes
            assert attributes["friendly_name"].startswith("Synthetic "), attributes
        extra = sorted(e for e in states if e not in TEMPERATURES and e not in OCCUPANCY)
        server.log("other entities: " + ", ".join(extra))
        devices = ["light", "switch", "climate", "camera", "media_player", "lock", "cover", "fan", "device_tracker", "alarm_control_panel", "notify"]
        assert not [e for e in extra if e.split(".")[0] in devices], extra

    def cgroup(unit):
        return "/sys/fs/cgroup" + server.succeed(f"systemctl show -p ControlGroup --value {unit}.service").strip()

    start_all()
    wait_for_demos()

    with subtest("Home Assistant reports only the labelled synthetic sensors"):
        server.succeed("demos-ha-test onboard")
        server.succeed("test $(stat -c %a /run/demos-test-token) = 600")
        server.fail("journalctl -u frigate --no-pager -o cat | grep -qi 'Password:'")
        wait_for_sensors()
        check_sensors()
        config = js("demos-ha-test config")
        assert config["location_name"] == "Terrace SYNTHETIC demo", config["location_name"]
        server.log("loaded components: " + ", ".join(sorted(config["components"])))
        for forbidden in ["cloud", "zeroconf", "ssdp", "dhcp", "usb", "bluetooth", "mqtt", "mobile_app",
                          "homeassistant_alerts", "esphome", "met", "demo"]:
            assert forbidden not in config["components"], forbidden
        # Analytics and local conversation are built-ins in the pinned release.
        # Verify privacy behavior, not the absence of a frontend dependency.
        server.wait_until_succeeds("demos-ha-test analytics", timeout=60)
        analytics = js("demos-ha-test analytics")
        assert analytics["preferences"] and not any(analytics["preferences"].values()), analytics

    with subtest("Frigate ingests the generated stream with detection and recording off"):
        wait_for_camera()
        camera = frigate_camera()
        server.log("camera stats: " + json.dumps(camera))
        assert camera["detection_fps"] == 0, camera
        # Read the frame Frigate serves to the UI and verify its actual pixels.
        pixels = server.succeed(
            f"curl -sf --max-time 10 {FRIGATE}/api/{CAMERA}/latest.jpg | "
            "${pkgs.ffmpeg-headless}/bin/ffmpeg -v error -threads 1 -i pipe:0 "
            "-vf scale=16:12,format=gray -frames:v 1 -f rawvideo pipe:1 | od -An -v -tu1"
        )
        samples = [int(value) for value in pixels.split()]
        assert len(samples) == 16 * 12 and max(samples) <= 5, "expected a black frame"
        streams = js(f"curl -sf --max-time 5 {GO2RTC}/api/streams")
        assert CAMERA in streams and streams[CAMERA]["consumers"], streams
        settings = js(f"curl -sf --max-time 5 {FRIGATE}/api/config")
        assert settings["mqtt"]["enabled"] is False
        assert settings["record"]["enabled"] is False
        assert settings["snapshots"]["enabled"] is False
        assert settings["detect"]["enabled"] is False
        assert settings["cameras"][CAMERA]["detect"]["enabled"] is False
        assert settings["cameras"][CAMERA]["record"]["enabled"] is False
        assert settings["telemetry"]["version_check"] is False
        assert list(settings["cameras"]) == [CAMERA]
        server.fail("find /var/lib/terrace-demo-frigate /var/cache/terrace-demo-frigate -name '*.mp4' | grep .")
        # Frigate's authenticated front door rejects anonymous callers.
        assert server.succeed("curl -s -o /dev/null -w '%{http_code}' -H 'Host: localhost' http://127.0.0.1/api/version").strip() == "401"

    with subtest("Operator status command passes and bounds are measured"):
        server.succeed("terrace-demo status")
        for unit in UNITS:
            usage = server.succeed(
                f"echo {unit} peak_bytes=$(cat {cgroup(unit)}/memory.peak) max_bytes=$(cat {cgroup(unit)}/memory.max)"
                f" cpu_usec=$(grep usage_usec {cgroup(unit)}/cpu.stat) tasks=$(cat {cgroup(unit)}/pids.current)"
                f" max_tasks=$(cat {cgroup(unit)}/pids.max)"
            )
            server.log("MEASURED " + usage.strip())
        server.log("MEASURED state " + server.succeed("du -sk /var/lib/terrace-demo-hass /var/lib/terrace-demo-frigate /var/cache/terrace-demo-frigate | tr '\\n' ' '").strip())

    with subtest("Nothing is reachable or listening beyond loopback"):
        pids = set()
        for unit in [*UNITS, "terrace-demo-ha"]:
            pids.update(server.succeed(f"find {cgroup(unit)} -name cgroup.procs -exec cat {{}} +").split())
        for unit in ["home-assistant", "terrace-demo-ha"]:
            pid = server.succeed(f"systemctl show {unit}.service -p MainPID --value").strip()
            interfaces = js(f"nsenter --target {pid} --net ip -j link show")
            assert [interface["ifname"] for interface in interfaces] == ["lo"], (unit, interfaces)
        listeners = server.succeed("ss -H -lntup")
        server.log(listeners)
        seen = set()
        for line in listeners.splitlines():
            owner = set(re.findall(r"pid=(\d+)", line))
            if not owner & pids:
                continue
            local = line.split()[4]
            host, port = local.rsplit(":", 1)
            seen.add(port)
            assert host in ("127.0.0.1", "[::1]"), line
        for port in ["8123", "5000", "5001", "80", "1984", "8554"]:
            assert port in seen, (port, seen)
        for port in [80, 5000, 5001, 8123, 1984, 8554, 8555, 1935, 8971, 8082, 5002]:
            client.fail(f"nc -z -w 3 server {port}")

    with subtest("Every unit has memory, CPU, task and network bounds"):
        for unit in UNITS:
            properties = dict(
                line.split("=", 1)
                for line in server.succeed(
                    f"systemctl show -p MemoryHigh -p MemoryMax -p MemorySwapMax -p CPUQuotaPerSecUSec -p TasksMax -p IPAddressDeny -p IPAddressAllow {unit}.service"
                ).splitlines()
            )
            for name in ["MemoryHigh", "MemoryMax", "CPUQuotaPerSecUSec", "TasksMax"]:
                assert properties[name] != "infinity", (unit, name, properties)
            assert properties["MemorySwapMax"] == "0", (unit, properties)
            assert "0.0.0.0/0" in properties["IPAddressDeny"] and "::/0" in properties["IPAddressDeny"], (unit, properties)
            assert "127.0.0.0/8" in properties["IPAddressAllow"], (unit, properties)
            assert server.succeed(f"cat {cgroup(unit)}/memory.max").strip().isdigit(), unit
        # Probe from each service's cgroup to exercise its installed packet filter.
        client.succeed("systemd-run --unit=probe-http python3 -m http.server 8000 --directory /etc")
        client.wait_for_open_port(8000)
        server.succeed("curl -sf --max-time 5 http://client:8000/hostname")
        for unit in UNITS:
            enter_group = f"echo $$ > {cgroup(unit)}/cgroup.procs"
            server.fail(f"sh -c '{enter_group} && exec curl -sf --max-time 3 http://client:8000/hostname'")
            server.succeed(f"sh -c '{enter_group} && exec curl -sf --max-time 3 http://127.0.0.1:8123/api/onboarding'")

    with subtest("Services recover from restart and keep synthetic state"):
        server.succeed("systemctl restart go2rtc.service frigate.service home-assistant.service")
        wait_for_demos()
        wait_for_camera()
        wait_for_sensors()
        check_sensors()
        assert not any(js("demos-ha-test analytics")["preferences"].values())
        server.succeed("terrace-demo status")

    with subtest("Teardown removes synthetic state and the demos restart empty"):
        server.fail("terrace-demo teardown")
        server.succeed("terrace-demo teardown --yes")
        for unit in UNITS:
            server.fail(f"systemctl is-active {unit}.service")
        server.succeed("test -z \"$(find /var/lib/terrace-demo-hass /var/lib/terrace-demo-frigate /var/cache/terrace-demo-frigate -mindepth 1)\"")
        server.succeed("systemctl start go2rtc.service frigate.service nginx.service home-assistant.service")
        wait_for_demos()
        wait_for_camera()
        steps = js(f"curl -sf --max-time 5 {HA}/api/onboarding")
        assert any(step["step"] == "user" and not step["done"] for step in steps), steps
        server.fail("demos-ha-test states")

    with subtest("Individual demo selection keeps status and teardown scoped"):
        server.succeed("/run/current-system/specialisation/ha-only/bin/switch-to-configuration test >&2")
        server.wait_for_unit("home-assistant.service")
        server.wait_until_succeeds("terrace-demo status", timeout=120)
        server.fail("systemctl is-active frigate.service")
        server.succeed("touch /var/lib/terrace-demo-frigate/retain-demo-marker")
        server.succeed("terrace-demo teardown --yes")
        server.succeed("test -f /var/lib/terrace-demo-frigate/retain-demo-marker")
        server.succeed("/run/booted-system/specialisation/frigate-only/bin/switch-to-configuration test >&2")
        server.wait_for_unit("frigate.service")
        wait_for_camera()
        server.succeed("terrace-demo status")
        server.fail("systemctl is-active home-assistant.service")
        server.succeed("touch /var/lib/terrace-demo-hass/retain-demo-marker")
        server.succeed("terrace-demo teardown --yes")
        server.succeed("test -f /var/lib/terrace-demo-hass/retain-demo-marker")

    with subtest("Disabling the module removes the services but retains their state"):
        server.succeed("/run/booted-system/specialisation/demos-off/bin/switch-to-configuration test >&2")
        for unit in UNITS:
            server.fail(f"systemctl is-active {unit}.service")
        for port in [8123, 5000, 5001, 1984, 8554]:
            server.fail(f"ss -H -ltn 'sport = :{port}' | grep .")
        server.succeed("test -f /var/lib/terrace-demo-hass/retain-demo-marker")
        server.fail("which terrace-demo")
        for path in ["/var/lib/hass", "/var/lib/frigate", "/var/cache/frigate"]:
            assert server.succeed(f"cat {path}/unrelated-marker").strip() == "retained"

  '';
}
