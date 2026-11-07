const SSHD_BOOTSTRAP = [
  "HostKey /etc/ssh/ssh_host_ed25519_key",
  "AllowUsers root",
  "PubkeyAuthentication yes",
  "AuthenticationMethods publickey",
  "PasswordAuthentication no",
  "KbdInteractiveAuthentication no",
  "HostbasedAuthentication no",
  "GSSAPIAuthentication no",
  "PermitRootLogin prohibit-password",
  "",
].join("\n");

export function renderBootstrapCloudConfig(
  hostPublicKey: string,
  hostPrivateKey: string,
  operatorPublicKey: string,
): string {
  return "#cloud-config\n" + JSON.stringify({
    users: [{ name: "root", lock_passwd: true, ssh_authorized_keys: [operatorPublicKey] }],
    disable_root: false,
    allow_public_ssh_keys: false,
    ssh_pwauth: false,
    ssh_deletekeys: true,
    ssh_genkeytypes: [],
    ssh_keys: { ed25519_public: hostPublicKey, ed25519_private: hostPrivateKey },
    ssh_quiet_keygen: true,
    ssh_publish_hostkeys: { enabled: false },
    ssh: { emit_keys_to_console: false },
    no_ssh_fingerprints: true,
    write_files: [{
      path: "/etc/ssh/sshd_config.d/00-terrace-bootstrap.conf",
      owner: "root:root",
      permissions: "0644",
      content: SSHD_BOOTSTRAP,
    }],
    runcmd: [["systemctl", "reload-or-restart", "ssh.service"]],
  }) + "\n";
}
