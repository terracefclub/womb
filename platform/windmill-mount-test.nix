{pkgs}: let
  release = builtins.fromJSON (builtins.readFile ./windmill/release.json);
  image = "${release.imageName}:${release.version}";
  imageFile = import ./windmill/image.nix {inherit pkgs;};
  workerMounts = import ./windmill/worker-mounts.nix {inherit image;};
  common = ["--pull=never" "--env=HOME=/tmp" "--network=none" "--userns=host" "--user=44123:44123" "--read-only" "--cap-drop=ALL" "--security-opt=no-new-privileges" "--pids-limit=64" "--cpus=1" "--memory=768m" "--timeout=20" "--stop-timeout=2"];
  probe = pkgs.writeText "windmill-mount-probe.sh" ''
    set -eu
    test "$(id -u)" = 44123
    test "$(stat -c '%u %a' /tmp/windmill)" = '44123 700'
    test "$(stat -c '%u %a' /tmp/windmill/cache)" = '44123 700'
    mkdir /tmp/windmill/logs
    touch /tmp/windmill/logs/proof /tmp/windmill/cache/proof
    rm /tmp/windmill/logs/proof /tmp/windmill/cache/proof
    interpreter=$(uv python find 3.12 --system --python-preference=only-managed)
    "$interpreter" -c 'import os,sys; assert sys.version_info[:2] == (3,12); assert os.path.realpath(sys.executable).startswith("/tmp/windmill/cache/py_runtime/"); assert os.statvfs(sys.executable).f_flag & os.ST_RDONLY; print("Native managed Python mount and writable parents passed.")'
  '';
  run = pkgs.lib.escapeShellArgs (common ++ workerMounts ++ ["--name=terrace-windmill-mount-probe" "--volume=${probe}:/run/mount-probe.sh:ro" "--entrypoint=/bin/sh" image "/run/mount-probe.sh"]);
  bad = pkgs.lib.escapeShellArgs (common ++ ["--name=terrace-windmill-bad-mount-probe" "--tmpfs=/tmp/windmill:mode=0700,uid=44123,gid=44123,size=128m" "--entrypoint=/bin/true" image]);
in {
  name = "terrace-windmill-mounts";
  nodes.host = {
    virtualisation = {
      memorySize = 2048;
      cores = 2;
      diskSize = 16384;
      podman.enable = true;
      qemu.options = ["-fw_cfg" "name=opt/terrace-workload,string=windmill-mounts"];
    };
    environment.systemPackages = [pkgs.podman pkgs.coreutils];
    environment.etc."windmill-image.tar".source = imageFile;
    system.stateVersion = "26.05";
  };
  testScript = ''
    import json, time
    start_all()
    host.wait_for_unit("multi-user.target")
    image_started = time.monotonic()
    image_status, _ = host.execute("timeout 180 podman load --input /etc/windmill-image.tar >/dev/null 2>&1", timeout=190)
    print("Windmill mount image load status: " + str(image_status))
    print("Windmill mount image load seconds: " + str(round(time.monotonic() - image_started, 2)))
    assert image_status == 0, "Pinned diagnostic image import did not complete."
    names = ["terrace-windmill-bad-mount-probe", "terrace-windmill-mount-probe"]
    def absent(name):
        status, _ = host.execute("timeout 5 podman container exists " + name)
        assert status == 1, "Exact diagnostic container absence could not be proved."
    for name in names:
        absent(name)
    try:
        with subtest("exact unsupported numeric ownership option"):
            status, error = host.execute("timeout 25 podman run ${bad} 2>&1", timeout=30)
            assert status == 125 and error.strip() == 'Error: unknown mount option "uid=44123": invalid mount option'
        with subtest("supported ownership, writes and read-only managed interpreter"):
            output = host.succeed("timeout 25 podman run ${run}", timeout=30)
            assert output.strip() == "Native managed Python mount and writable parents passed."
            user = json.loads(host.succeed("timeout 5 podman inspect --format '{{json .Config.User}}' terrace-windmill-mount-probe"))
            readonly = json.loads(host.succeed("timeout 5 podman inspect --format '{{json .HostConfig.ReadonlyRootfs}}' terrace-windmill-mount-probe"))
            mounts = json.loads(host.succeed("timeout 5 podman inspect --format '{{json .Mounts}}' terrace-windmill-mount-probe"))
            runtime = [row for row in mounts if row.get("Type") == "image" and row.get("Destination") == "/tmp/windmill/cache/py_runtime"]
            facts = {"userMatches": user == "44123:44123", "rootfsReadonly": readonly is True,
                     "runtimeMountCount": len(runtime), "runtimeReadonly": len(runtime) == 1 and runtime[0].get("RW") is False,
                     "runtimeImageType": len(runtime) == 1 and runtime[0].get("Type") == "image"}
            print("Windmill mount same-destination entries: " + str(sum(row.get("Destination") == "/tmp/windmill/cache/py_runtime" for row in mounts)))
            print("Windmill mount inspect facts: " + json.dumps(facts, sort_keys=True))
            assert facts == {"userMatches":True,"rootfsReadonly":True,"runtimeMountCount":1,"runtimeReadonly":True,"runtimeImageType":True}
    finally:
        for name in names:
            host.succeed("timeout 10 podman rm --force --ignore " + name, timeout=15)
            absent(name)
    with subtest("no application, identity or database was started"):
        assert host.succeed("timeout 5 podman ps --quiet").strip() == ""
    host.succeed("sync")
  '';
}
