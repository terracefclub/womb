import * as hcloud from "@pulumi/hcloud";
import * as pulumi from "@pulumi/pulumi";
import { cohortRoles, readBootstrapUserData, type RehearsalInput } from "./input";

type Owned = {
  servers: number[];
  primary_ips: number[];
  firewalls: number[];
  ssh_keys: number[];
};

function providerId(value: string): number {
  const id = Number(value);
  if (!Number.isSafeInteger(id) || id < 1 || String(id) !== value) {
    throw new Error("invalid rehearsal provider ID");
  }
  return id;
}

export function declareResources(input: RehearsalInput, inputPath?: string): pulumi.Output<Owned> {
  const prefix = "terrace-r-" + input.run;
  const labels = { "terrace-rehearsal": input.run };
  if (input.phase === "foundation") {
    const key = new hcloud.SshKey("key", {
      name: prefix + "-key",
      labels,
      publicKey: input.sshPublicKey,
    });
    const firewall = new hcloud.Firewall("firewall", {
      name: prefix + "-firewall",
      labels,
      rules: [{ direction: "in", protocol: "tcp", port: "22", sourceIps: input.sshCidrs }],
    });
    return pulumi.output({
      servers: [],
      primary_ips: [],
      firewalls: [firewall.id.apply(providerId)],
      ssh_keys: [key.id.apply(providerId)],
    });
  }

  const bootstrap = readBootstrapUserData(inputPath, input);
  const hosts = cohortRoles(input.probeOnly).map(role => {
    const name = prefix + "-" + input.phase + "-" + role;
    const ip = new hcloud.PrimaryIp(role + "-ip", {
      name: name + "-ip",
      labels,
      location: input.location,
      type: "ipv4",
      autoDelete: false,
      deleteProtection: false,
    });
    const ipId = ip.id.apply(providerId);
    const userData = bootstrap[role];
    const server = new hcloud.Server(role, {
      name,
      labels,
      location: input.location,
      image: input.image,
      serverType: role === "app" ? input.appType : input.relayType,
      sshKeys: [String(input.sshKeyId)],
      firewallIds: [input.firewallId],
      publicNets: [{ ipv4Enabled: true, ipv4: ipId, ipv6Enabled: false }],
      backups: false,
      deleteProtection: false,
      rebuildProtection: false,
      ...(userData ? { userData: pulumi.secret(userData) } : {}),
    }, userData ? { additionalSecretOutputs: ["userData"] } : undefined);
    return { ipId, serverId: server.id.apply(providerId) };
  });
  return pulumi.output({
    servers: hosts.map(host => host.serverId),
    primary_ips: hosts.map(host => host.ipId),
    firewalls: [],
    ssh_keys: [],
  });
}
