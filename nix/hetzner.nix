{config, ...}: let
  inherit (config.canivete.meta) root;
in {
  canivete.deploy.nixos.nodes.${root} = let
    host = "\${ hcloud_primary_ip.ipv4.ip_address }";
  in {
    # TODO is there a way to improve this UI?
    install.host = host;
    build.host = host;
    build.sshOptions = ["User=root"];
    target.host = host;
    target.sshOptions = ["User=root"];
  };
  perSystem.canivete.opentofu.workspaces.deploy = {
    plugins = ["hetznercloud/hcloud"];
    modules.main = {
      canivete,
      lib,
      ...
    }: {
      provider.hcloud.token = canivete.vals.sops "default.yaml#/hetzner";
      resource = {
        hcloud_ssh_key.tristan = {
          name = "tristan";
          public_key = lib.fileContents ./ssh/tristan.pub;
        };
        hcloud_primary_ip.ipv4 = {
          name = "ipv4";
          type = "ipv4";
          assignee_type = "server";
          datacenter = "ash-dc1";
          auto_delete = false;
        };
        hcloud_primary_ip.ipv6 = {
          name = "ipv6";
          type = "ipv6";
          assignee_type = "server";
          datacenter = "ash-dc1";
          auto_delete = false;
        };
        hcloud_server.${root} = {
          depends_on = ["hcloud_ssh_key.tristan"];
          name = root;
          location = "ash";
          server_type = "cpx11";
          image = "debian-12";
          keep_disk = true;
          ssh_keys = ["tristan"];
          public_net.ipv4 = "\${ hcloud_primary_ip.ipv4.id }";
          public_net.ipv6 = "\${ hcloud_primary_ip.ipv6.id }";

          lifecycle.ignore_changes = ["ssh_keys"];
          lifecycle.prevent_destroy = true;
        };

        null_resource."nixos_${root}_system_install".depends_on = ["hcloud_server.${root}"];
      };
    };
  };
}
