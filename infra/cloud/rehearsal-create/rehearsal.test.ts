import { expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { chmodSync, linkSync, mkdtempSync, realpathSync, rmSync, symlinkSync, unlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import * as pulumi from "@pulumi/pulumi";
import { isRpcSecret, unwrapRpcSecret } from "@pulumi/pulumi/runtime/rpc";
import { renderBootstrapCloudConfig } from "./bootstrap";
import { parseInput, readBootstrapUserData, readInput } from "./input";
import { declareResources } from "./resources";

const run = "a".repeat(32);
const prefix = "terrace-r-" + run;
const keyBlob = Buffer.concat([
  Buffer.from("0000000b7373682d6564323535313900000020", "hex"),
  Buffer.alloc(32, 1),
]);
const sshPublicKey = "ssh-ed25519 " + keyBlob.toString("base64");
const foundation = { run, phase: "foundation" as const, sshPublicKey, sshCidrs: ["192.0.2.30/32"] };
const cohort = {
  run, phase: "original" as const, sshKeyId: 11, firewallId: 12, image: "debian-13" as const,
  location: "nbg1", appType: "cx43", relayType: "cx23",
};
const syntheticPrivate = "-----BEGIN OPENSSH PRIVATE KEY-----\nZml4dHVyZQ==\n-----END OPENSSH PRIVATE KEY-----\n";
const hostKey = (byte: number, privateKeySha256: string) => ({
  publicKey: "ssh-ed25519 " + Buffer.concat([keyBlob.subarray(0, 19), Buffer.alloc(32, byte)]).toString("base64"),
  privateKeySha256,
});
const relayKey = hostKey(2, createHash("sha256").update(syntheticPrivate).digest("hex"));
const bootstrapFields = { bootstrapHostKeys: { relay: relayKey }, bootstrapOperatorPublicKey: sshPublicKey };

test("rejects mismatched leases and unsupported resource inputs", () => {
  for (const changed of [
    { run: run + "\n" }, { phase: "replacement" }, { sshKeyId: "11" },
    { firewallId: 0 }, { image: "custom-snapshot" }, { location: "ash" },
    { appType: "cax31" }, { backups: true },
  ]) {
    expect(() => parseInput({ ...cohort, ...changed }, "original")).toThrow();
  }
  expect(() => parseInput(cohort, "production")).toThrow();
  for (const changed of [
    { sshCidrs: ["0.0.0.0/0"] }, { sshCidrs: ["::1/128"] },
    { sshCidrs: ["192.0.2.30/32", "192.0.2.30/32"] },
    { sshPublicKey: "synthetic-private-material" },
  ]) {
    expect(() => parseInput({ ...foundation, ...changed }, "foundation")).toThrow();
  }
});

test("reads bounded private regular input without following symlinks", () => {
  const directory = mkdtempSync(join(tmpdir(), "terrace-stack-test-"));
  try {
    const path = join(directory, "input.json");
    writeFileSync(path, JSON.stringify(foundation), { mode: 0o600 });
    expect(readInput(path, "foundation")).toEqual(foundation);
    const link = join(directory, "link.json");
    symlinkSync(path, link);
    expect(() => readInput(link, "foundation")).toThrow();
    const publicPath = join(directory, "public.json");
    writeFileSync(publicPath, JSON.stringify(foundation), { mode: 0o644 });
    expect(() => readInput(publicPath, "foundation")).toThrow();
    writeFileSync(path, "x".repeat(4097));
    expect(() => readInput(path, "foundation")).toThrow();
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("allows only explicit original console probe mode", () => {
  expect(parseInput(cohort, "original")).toEqual(cohort);
  expect(parseInput({ ...cohort, probeOnly: true }, "original")).toEqual({ ...cohort, probeOnly: true });
  for (const probeOnly of [false, null, "true", 1, undefined]) {
    expect(() => parseInput({ ...cohort, probeOnly }, "original")).toThrow();
  }
  expect(() => parseInput({ ...cohort, phase: "replacement", probeOnly: true }, "replacement")).toThrow();
  expect(() => parseInput({ ...foundation, probeOnly: true }, "foundation")).toThrow();
});

test("requires exactly the planned bootstrap roles and distinct canonical public keys", () => {
  const probe = { ...cohort, probeOnly: true as const, ...bootstrapFields };
  expect(parseInput(probe, "original")).toEqual(probe);
  const full = { ...cohort, phase: "replacement" as const,
    ...bootstrapFields, bootstrapHostKeys: { app: hostKey(3, "a".repeat(64)), relay: relayKey } };
  expect(parseInput(full, "replacement")).toEqual(full);
  expect(() => parseInput({ ...foundation, ...bootstrapFields }, "foundation")).toThrow();
  for (const changes of [
    { bootstrapOperatorPublicKey: undefined },
    { bootstrapOperatorPublicKey: sshPublicKey + " comment" },
    { bootstrapHostKeys: {} },
    { bootstrapHostKeys: { relay: { ...relayKey, publicKey: sshPublicKey } } },
    { bootstrapHostKeys: { relay: { ...relayKey, privateKeySha256: "A".repeat(64) } } },
    { bootstrapHostKeys: { relay: { ...relayKey, path: "outside" } } },
    { bootstrapHostKeys: full.bootstrapHostKeys },
  ]) {
    expect(() => parseInput({ ...probe, ...changes }, "original")).toThrow();
  }
  expect(() => parseInput({ ...full, bootstrapHostKeys: { relay: relayKey, app: relayKey } }, "replacement")).toThrow();
  expect(() => parseInput({ ...full, bootstrapHostKeys: {
    relay: relayKey, app: hostKey(3, relayKey.privateKeySha256),
  } }, "replacement")).toThrow();
  expect(() => parseInput({ ...full, bootstrapHostKeys: { relay: relayKey } }, "replacement")).toThrow();
});

test("only reads the exact bound private sibling and renders one root key", () => {
  const directory = mkdtempSync(join(realpathSync(tmpdir()), "terrace-bootstrap-test-"));
  try {
    const inputPath = join(directory, "original-input.json");
    const privatePath = join(directory, "original-relay-bootstrap-ed25519");
    writeFileSync(inputPath, JSON.stringify({ ...cohort, probeOnly: true, ...bootstrapFields }), { mode: 0o600 });
    const input = readInput(inputPath, "original");
    if (input.phase !== "original") throw new Error("wrong fixture phase");
    writeFileSync(privatePath, syntheticPrivate, { mode: 0o600 });
    const expected = renderBootstrapCloudConfig(relayKey.publicKey, syntheticPrivate, sshPublicKey);
    expect(readBootstrapUserData(inputPath, input)).toEqual({ relay: expected });
    expect(expected.startsWith("#cloud-config\n")).toBe(true);
    const config = JSON.parse(expected.slice("#cloud-config\n".length));
    expect(config).not.toHaveProperty("ssh_genkeytypes");
    expect(config).toEqual({
      users: [{ name: "root", lock_passwd: true, ssh_authorized_keys: [sshPublicKey] }],
      disable_root: false, allow_public_ssh_keys: false, ssh_pwauth: false,
      ssh_deletekeys: true,
      ssh_keys: { ed25519_public: relayKey.publicKey, ed25519_private: syntheticPrivate },
      ssh_quiet_keygen: true, ssh_publish_hostkeys: { enabled: false },
      ssh: { emit_keys_to_console: false }, no_ssh_fingerprints: true,
      write_files: [{
        path: "/etc/ssh/sshd_config.d/00-terrace-bootstrap.conf", owner: "root:root", permissions: "0644",
        content: "HostKey /etc/ssh/ssh_host_ed25519_key\nAllowUsers root\nPubkeyAuthentication yes\n"
          + "AuthenticationMethods publickey\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n"
          + "HostbasedAuthentication no\nGSSAPIAuthentication no\nPermitRootLogin prohibit-password\n",
      }],
      runcmd: [["systemctl", "reload-or-restart", "ssh.service"]],
    });
    expect(() => readBootstrapUserData(join(directory, "replacement-input.json"), input)).toThrow();
    chmodSync(directory, 0o755);
    expect(() => readBootstrapUserData(inputPath, input)).toThrow();
    chmodSync(directory, 0o700);
    chmodSync(privatePath, 0o644);
    expect(() => readBootstrapUserData(inputPath, input)).toThrow();
    chmodSync(privatePath, 0o600);
    const extra = join(directory, "other");
    linkSync(privatePath, extra);
    expect(() => readBootstrapUserData(inputPath, input)).toThrow();
    unlinkSync(extra);
    writeFileSync(privatePath, "x".repeat(2049));
    expect(() => readBootstrapUserData(inputPath, input)).toThrow();
    writeFileSync(privatePath, syntheticPrivate + "\n");
    expect(() => readBootstrapUserData(inputPath, input)).toThrow();
    unlinkSync(privatePath);
    writeFileSync(extra, syntheticPrivate, { mode: 0o600 });
    symlinkSync(extra, privatePath);
    expect(() => readBootstrapUserData(inputPath, input)).toThrow();
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("marks provider user data secret before registering the only probe server", async () => {
  const directory = mkdtempSync(join(realpathSync(tmpdir()), "terrace-bootstrap-secret-test-"));
  try {
    const resources = new Map<string, pulumi.runtime.MockResourceArgs>();
    await pulumi.runtime.setMocks({
      newResource(args) {
        resources.set(args.name, args);
        return { id: args.name === "relay" ? "16" : "14", state: args.inputs };
      },
      call() {
        throw new Error("provider lookup is outside the rehearsal declarations");
      },
    }, "terrace-cloud-rehearsal", "original", false);
    const inputPath = join(directory, "original-input.json");
    writeFileSync(inputPath, JSON.stringify({ ...cohort, probeOnly: true, ...bootstrapFields }), { mode: 0o600 });
    writeFileSync(join(directory, "original-relay-bootstrap-ed25519"), syntheticPrivate, { mode: 0o600 });
    const output = declareResources(readInput(inputPath, "original"), inputPath);
    expect(await new Promise(resolve => output.apply(resolve))).toEqual({
      servers: [16], primary_ips: [14], firewalls: [], ssh_keys: [],
    });
    expect([...resources.keys()].sort()).toEqual(["relay", "relay-ip"]);
    const userData = resources.get("relay")?.inputs.userData;
    expect(isRpcSecret(userData)).toBe(true);
    expect(unwrapRpcSecret(userData)).toEqual(renderBootstrapCloudConfig(relayKey.publicKey, syntheticPrivate, sshPublicKey));
    expect(resources.get("relay-ip")?.inputs.userData).toBeUndefined();
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test.each(["foundation", "original", "replacement", "console probe"] as const)("declares only the %s cohort", async mode => {
  const phase = mode === "console probe" ? "original" : mode;
  const resources = new Map<string, pulumi.runtime.MockResourceArgs>();
  const ids: Record<string, string> = {
    key: "11", firewall: "12", "app-ip": "13", "relay-ip": "14", app: "15", relay: "16",
  };
  await pulumi.runtime.setMocks({
    newResource(args) {
      resources.set(args.name, args);
      return { id: ids[args.name], state: args.inputs };
    },
    call() {
      throw new Error("provider lookup is outside the rehearsal declarations");
    },
  }, "terrace-cloud-rehearsal", phase, false);

  const value = phase === "foundation" ? foundation
    : { ...cohort, phase, ...(mode === "console probe" ? { probeOnly: true } : {}) };
  const output = declareResources(parseInput(value, phase));
  const owned = await new Promise(resolve => output.apply(resolve));
  const labels = { "terrace-rehearsal": run };

  if (phase === "foundation") {
    expect([...resources.keys()].sort()).toEqual(["firewall", "key"]);
    expect(resources.get("key")?.type).toBe("hcloud:index/sshKey:SshKey");
    expect(resources.get("key")?.inputs).toEqual({ name: prefix + "-key", publicKey: sshPublicKey, labels });
    expect(resources.get("firewall")?.type).toBe("hcloud:index/firewall:Firewall");
    expect(resources.get("firewall")?.inputs).toEqual({
      name: prefix + "-firewall", labels,
      rules: [{ direction: "in", protocol: "tcp", port: "22", sourceIps: foundation.sshCidrs }],
    });
    expect(owned).toEqual({ servers: [], primary_ips: [], firewalls: [12], ssh_keys: [11] });
    return;
  }

  const roles = mode === "console probe" ? (["relay"] as const) : (["app", "relay"] as const);
  expect([...resources.keys()].sort()).toEqual(roles.flatMap(role => [role, role + "-ip"]));
  for (const role of roles) {
    const name = prefix + "-" + phase + "-" + role;
    const ip = resources.get(role + "-ip");
    expect(ip?.type).toBe("hcloud:index/primaryIp:PrimaryIp");
    expect(ip?.inputs).toEqual({
      name: name + "-ip", labels, type: "ipv4", location: cohort.location,
      autoDelete: false, deleteProtection: false,
    });
    const server = resources.get(role);
    expect(server?.type).toBe("hcloud:index/server:Server");
    expect(server?.inputs).toEqual({
      name, labels, location: cohort.location, image: cohort.image,
      serverType: role === "app" ? cohort.appType : cohort.relayType,
      sshKeys: ["11"], firewallIds: [12],
      publicNets: [{ ipv4Enabled: true, ipv4: Number(ids[role + "-ip"]), ipv6Enabled: false }],
      backups: false, deleteProtection: false, rebuildProtection: false,
    });
  }
  expect(owned).toEqual(mode === "console probe"
    ? { servers: [16], primary_ips: [14], firewalls: [], ssh_keys: [] }
    : { servers: [15, 16], primary_ips: [13, 14], firewalls: [], ssh_keys: [] });
});
