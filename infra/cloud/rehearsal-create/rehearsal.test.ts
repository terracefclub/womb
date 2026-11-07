import { expect, test } from "bun:test";
import { mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import * as pulumi from "@pulumi/pulumi";
import { parseInput, readInput } from "./input";
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
  run, phase: "original", sshKeyId: 11, firewallId: 12, image: "debian-13",
  location: "nbg1", appType: "cx43", relayType: "cx23",
};

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
