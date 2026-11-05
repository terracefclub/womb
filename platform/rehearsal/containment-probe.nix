{runId}: let
  platform = builtins.getFlake (toString ../.);
  pkgs = import platform.inputs.nixpkgs {system = "x86_64-linux";};
in
  assert builtins.match "[a-f0-9]{32}" runId != null;
    pkgs.testers.runNixOSTest {
      name = "terrace-contained-${runId}";
      nodes.probe = {...}: {
        virtualisation = {
          memorySize = 512;
          cores = 1;
          qemu.options = ["-fw_cfg" "name=opt/terrace-rehearsal,string=${runId}"];
        };
      };
      testScript = ''
        import time
        start_all()
        probe.wait_for_unit("multi-user.target")
        print("REHEARSAL_PROBE_READY", flush=True)
        time.sleep(600)
      '';
    }
