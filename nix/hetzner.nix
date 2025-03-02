{config, ...}: let
  inherit (config.canivete.meta) root;
in {
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
          type = "ipv4";
          assignee_type = "server";
          auto_delete = false;
        };
        hcloud_primary_ip.ipv6 = {
          type = "ipv6";
          assignee_type = "server";
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

          lifecycle.ignore_changes = ["ssh_keys"];
          lifecycle.prevent_destroy = true;
        };
      };
    };
  };
}
