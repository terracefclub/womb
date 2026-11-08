// The host generates its own Ed25519 key on first boot. User data carries only a
// one-use proof nonce: the host publishes HMAC(nonce, context and public key) as
// its pre-authentication SSH banner, and the operator pins the key only when the
// MAC verifies for the key presented in that same session. No private key ever
// passes through user data, the Pulumi plan or its state.
export const PROOF_BANNER = "/etc/ssh/terrace-hostkey-proof";
const PROOF_INPUT = "/run/terrace-bootstrap/proof.json";

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
  "Banner " + PROOF_BANNER,
  "",
].join("\n");

// Runs once in cloud-init's final stage, after the SSH module generated the key.
const PROOF_SCRIPT = `import hashlib, hmac, json, os
source = "${PROOF_INPUT}"
with open(source) as stream:
    proof = json.load(stream)
os.unlink(source)
with open("/etc/ssh/ssh_host_ed25519_key.pub") as stream:
    kind, key = stream.read().split()[:2]
assert kind == "ssh-ed25519"
message = "|".join(("terrace-hostkey-v1", proof["run"], proof["phase"], proof["role"], kind + " " + key))
mac = hmac.new(bytes.fromhex(proof["nonce"]), message.encode(), hashlib.sha256).hexdigest()
temporary = "${PROOF_BANNER}.tmp"
with open(temporary, "w") as stream:
    stream.write("terrace-hostkey-proof v1 " + mac + "\\n")
os.chmod(temporary, 0o644)
os.replace(temporary, "${PROOF_BANNER}")
`;

export type HostKeyProof = { nonce: string; run: string; phase: string; role: string };

export function renderBootstrapCloudConfig(proof: HostKeyProof, operatorPublicKey: string): string {
  if (!/^[a-f0-9]{64}$/.test(proof.nonce) || !/^[a-f0-9]{32}$/.test(proof.run)
    || !/^(original|replacement)$/.test(proof.phase) || !/^(app|relay)$/.test(proof.role)) {
    throw new Error("invalid host key proof context");
  }
  return "#cloud-config\n" + JSON.stringify({
    users: [{ name: "root", lock_passwd: true, ssh_authorized_keys: [operatorPublicKey] }],
    disable_root: false,
    allow_public_ssh_keys: false,
    ssh_pwauth: false,
    ssh_deletekeys: true,
    ssh_genkeytypes: ["ed25519"],
    ssh_quiet_keygen: true,
    ssh_publish_hostkeys: { enabled: false },
    ssh: { emit_keys_to_console: false },
    no_ssh_fingerprints: true,
    write_files: [{
      path: "/etc/ssh/sshd_config.d/00-terrace-bootstrap.conf",
      owner: "root:root",
      permissions: "0644",
      content: SSHD_BOOTSTRAP,
    }, {
      path: PROOF_INPUT,
      owner: "root:root",
      permissions: "0600",
      content: JSON.stringify({ nonce: proof.nonce, run: proof.run, phase: proof.phase, role: proof.role }),
    }],
    runcmd: [["python3", "-c", PROOF_SCRIPT], ["systemctl", "reload-or-restart", "ssh.service"]],
  }) + "\n";
}
