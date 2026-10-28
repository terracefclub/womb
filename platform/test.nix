{
  pkgs,
  module,
}: let
  probeImage = pkgs.dockerTools.buildImage {
    name = "terrace-persistence-probe";
    tag = "v1";
    copyToRoot = pkgs.buildEnv {
      name = "terrace-probe-root";
      paths = [pkgs.pkgsStatic.busybox];
      pathsToLink = ["/bin"];
    };
  };
  workload = pkgs.writeText "terrace-persistence-probe.yaml" ''
    apiVersion: v1
    kind: PersistentVolumeClaim
    metadata:
      name: probe
    spec:
      accessModes: [ReadWriteOnce]
      storageClassName: local-retain
      resources:
        requests:
          storage: 16Mi
    ---
    apiVersion: v1
    kind: Pod
    metadata:
      name: probe
    spec:
      containers:
        - name: probe
          image: terrace-persistence-probe:v1
          imagePullPolicy: Never
          command: ["/bin/busybox", "sleep", "3600"]
          volumeMounts:
            - name: data
              mountPath: /data
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: probe
  '';
in {
  name = "terrace-single-node";
  nodes = {
    server = {...}: {
      imports = [module];
      terrace.singleNode.enable = true;
      services.k3s.images = [pkgs.k3s.airgap-images probeImage];
      virtualisation.memorySize = 4096;
      virtualisation.cores = 2;
    };
    client = {...}: {
      environment.systemPackages = [pkgs.curl];
    };
  };
  testScript = ''
    start_all()
    server.wait_for_unit("k3s.service")
    server.wait_until_succeeds("k3s kubectl get nodes --no-headers | grep ' Ready '", timeout=360)
    server.wait_until_succeeds("k3s kubectl get storageclass local-retain -o jsonpath='{.reclaimPolicy}' | grep -qx Retain", timeout=180)
    client.fail("curl --connect-timeout 3 --max-time 5 -k https://server:6443/version")
    server.succeed("k3s kubectl apply -f ${workload}")
    server.wait_until_succeeds("k3s kubectl wait --for=condition=Ready pod/probe --timeout=5s", timeout=360)
    server.succeed("k3s kubectl exec probe -- /bin/busybox sh -c 'echo retained > /data/marker'")
    volume = server.succeed("k3s kubectl get pvc probe -o jsonpath='{.spec.volumeName}'").strip()
    server.succeed("systemctl restart k3s.service")
    server.wait_until_succeeds("k3s kubectl get nodes --no-headers | grep ' Ready '", timeout=360)
    server.wait_until_succeeds("k3s kubectl exec probe -- /bin/busybox cat /data/marker | grep -qx retained", timeout=360)
    server.succeed("k3s kubectl delete pod probe --wait=true")
    server.succeed("k3s kubectl apply -f ${workload}")
    server.wait_until_succeeds("k3s kubectl wait --for=condition=Ready pod/probe --timeout=5s", timeout=360)
    server.succeed("k3s kubectl exec probe -- /bin/busybox cat /data/marker | grep -qx retained")
    server.succeed("k3s kubectl delete pod probe --wait=true")
    server.succeed("k3s kubectl delete pvc probe --wait=true")
    server.wait_until_succeeds("k3s kubectl get pv " + volume + " -o jsonpath='{.status.phase}' | grep -qx Released", timeout=180)
    path = server.succeed("k3s kubectl get pv " + volume + " -o jsonpath='{.spec.hostPath.path}'").strip()
    assert path.startswith("/var/lib/rancher/k3s/storage/")
    server.succeed("test \"$(cat '" + path + "/marker')\" = retained")
  '';
}
