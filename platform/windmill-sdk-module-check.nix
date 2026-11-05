let
  platform = builtins.getFlake (toString ./.);
  lib = platform.inputs.nixpkgs.lib;
  machine = lib.nixosSystem {
    system = "x86_64-linux";
    modules = [
      platform.nixosModules.windmill
      {
        system.stateVersion = "26.05";
        terrace.windmill = {
          enable = true;
          membershipRuntime = true;
          hostname = "windmill.terrace.invalid";
          certificateFile = "/var/lib/terrace-windmill-inputs/cert.pem";
          certificateKeyFile = "/var/lib/terrace-windmill-inputs/key.pem";
        };
      }
    ];
  };
  containers = machine.config.virtualisation.oci-containers.containers;
  worker = containers.terrace-windmill-worker;
  sdk = builtins.filter (option: lib.hasInfix "/run/terrace-membership-python" option) worker.extraOptions;
in
  assert machine.options.terrace.windmill.membershipRuntime.default == false;
  assert builtins.length sdk == 1;
  assert builtins.match "--volume=/nix/store/[a-z0-9]{32}-terrace-windmill-membership-python312:/run/terrace-membership-python:ro" (builtins.head sdk) != null;
  assert worker.environment.ADDITIONAL_PYTHON_PATHS == "/run/terrace-membership-python";
  assert builtins.length (lib.splitString "," worker.environment.PIP_LOCAL_DEPENDENCIES) == 20;
  assert !(containers.terrace-windmill-server.environment ? ADDITIONAL_PYTHON_PATHS); "Exported Windmill SDK module resolves without repository-root files."
