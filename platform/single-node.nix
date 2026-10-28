{
  config,
  lib,
  ...
}: let
  cfg = config.terrace.singleNode;
in {
  options.terrace.singleNode = {
    enable = lib.mkEnableOption "Terrace single-node Kubernetes platform";
  };

  config = lib.mkIf cfg.enable {
    services.k3s = {
      enable = true;
      role = "server";
      clusterInit = false; # SQLite, not embedded etcd, for one server.
      disable = ["traefik" "servicelb"];
      manifests.local-retain.content = {
        apiVersion = "storage.k8s.io/v1";
        kind = "StorageClass";
        metadata.name = "local-retain";
        provisioner = "rancher.io/local-path";
        reclaimPolicy = "Retain";
        volumeBindingMode = "WaitForFirstConsumer";
      };
    };

    networking.firewall = {
      enable = true;
      # K3s API 6443 stays closed on the host. Pods need the local CNI interfaces.
      trustedInterfaces = ["cni0" "flannel.1"];
    };
  };
}
