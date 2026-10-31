import * as fs from "node:fs";
import * as path from "node:path";
import { createHash } from "node:crypto";
import { isIP } from "node:net";
import * as hcloud from "@pulumi/hcloud";
import * as pulumi from "@pulumi/pulumi";
import {
  DEFAULT_PLAN,
  assessQuote,
  euLocation,
  percentBasisPoints,
  serverType,
  usdCents,
} from "./budget";

const config = new pulumi.Config();

// Hetzner tokens are scoped to a project. The name still needs console verification.
if (config.require("dedicatedProjectConfirmed") !== "terrace" || !process.env.HCLOUD_TOKEN) {
  throw new Error("verify the dedicated Terrace Hetzner project and provide its token at runtime");
}
const expectedTokenSha256 = config.require("tokenSha256");
if (!/^[a-f0-9]{64}$/.test(expectedTokenSha256) ||
  createHash("sha256").update(process.env.HCLOUD_TOKEN).digest("hex") !== expectedTokenSha256) {
  throw new Error("runtime Hetzner token does not match the pinned Terrace-project token fingerprint");
}

// Defaults encode the reviewed plan in docs/access-relay.md. Overrides are
// allowed but must still pass the strict US$80 budget gate below.
const location = euLocation("location", config.get("location") ?? DEFAULT_PLAN.appLocation);
const relayLocation = euLocation("relayLocation", config.get("relayLocation") ?? DEFAULT_PLAN.relayLocation);
const relayType = serverType("relayServerType", config.get("relayServerType") ?? DEFAULT_PLAN.relayServerType);
const appType = serverType("appServerType", config.get("appServerType") ?? DEFAULT_PLAN.appServerType);
const relayProviderBackups = config.getBoolean("relayProviderBackups") ?? DEFAULT_PLAN.relayProviderBackups;
const appProviderBackups = config.getBoolean("appProviderBackups") ?? DEFAULT_PLAN.appProviderBackups;
const bootstrapImage = config.require("bootstrapImage");
if (!/^debian-[0-9]+$/.test(bootstrapImage)) {
  throw new Error("bootstrapImage must be a current Debian image name checked in the project");
}

const sshCidrs = config.require("sshCidrs").split(",").map((cidr) => cidr.trim());
if (sshCidrs.length === 0 || sshCidrs.some((cidr) => {
  const slash = cidr.lastIndexOf("/");
  const address = cidr.slice(0, slash);
  const bits = Number(cidr.slice(slash + 1));
  const family = isIP(address);
  return slash < 0 || !Number.isInteger(bits) ||
    (family === 4 ? bits < 24 || bits > 32 : family === 6 ? bits < 64 || bits > 128 : true);
})) {
  throw new Error("sshCidrs requires explicit narrow IPv4 or IPv6 CIDRs");
}

const publicKeyFile = config.require("operatorPublicKeyFile");
if (!path.isAbsolute(publicKeyFile) || !publicKeyFile.endsWith(".pub")) {
  throw new Error("operatorPublicKeyFile must be an absolute .pub path");
}
let publicKey: string;
try {
  publicKey = fs.readFileSync(publicKeyFile, "utf8").trim();
} catch {
  throw new Error("operator public key file could not be read");
}
if (!/^(ssh-ed25519|ecdsa-sha2-[^ ]+) [A-Za-z0-9+/=]+(?: .*)?$/.test(publicKey)) {
  throw new Error("operator public key file is not a supported SSH public key");
}

const quote = assessQuote({
  date: config.require("quoteDate"),
  relayServerCents: usdCents(config.require("relayServerMonthlyUsd"), "relayServerMonthlyUsd"),
  appServerCents: usdCents(config.require("appServerMonthlyUsd"), "appServerMonthlyUsd"),
  relayProviderBackups,
  appProviderBackups,
  ipv4Cents: usdCents(config.require("ipv4MonthlyUsd"), "ipv4MonthlyUsd"),
  offsiteBackupCents: usdCents(config.require("offsiteBackupMonthlyUsd"), "offsiteBackupMonthlyUsd"),
  existingServerCents: usdCents(config.require("existingServerMonthlyUsd"), "existingServerMonthlyUsd"),
  otherCents: usdCents(config.require("otherMonthlyUsd"), "otherMonthlyUsd"),
  taxBasisPoints: percentBasisPoints(config.require("taxPercent")),
});

const labels = { owner: "terrace", stage: "bootstrap" };
const firewall = new hcloud.Firewall("bootstrap-firewall", {
  name: "terrace-bootstrap",
  labels,
  rules: [{ direction: "in", protocol: "tcp", port: "22", sourceIps: sshCidrs }],
});
const sshKey = new hcloud.SshKey("operator", {
  name: "terrace-operator",
  publicKey,
  labels,
});
const baseServer = {
  image: bootstrapImage,
  deleteProtection: true,
  rebuildProtection: true,
  firewallIds: [firewall.id.apply(Number)],
  sshKeys: [sshKey.name],
  publicNets: [{ ipv4Enabled: true, ipv6Enabled: false }],
};
const relay = new hcloud.Server("relay-server", {
  ...baseServer,
  name: "terrace-relay-1",
  location: relayLocation,
  serverType: relayType,
  backups: relayProviderBackups,
  labels: { ...labels, role: "relay" },
}, { protect: true });
const app = new hcloud.Server("app-server", {
  ...baseServer,
  name: "terrace-app-1",
  location,
  serverType: appType,
  backups: appProviderBackups,
  labels: { ...labels, role: "app" },
}, { protect: true });

export const quotedSteadyMonthlyUsd = (quote.steadyCents / 100).toFixed(2);
export const quotedOverlapMonthlyUsd = (quote.overlapCents / 100).toFixed(2);
export const relayServerId = relay.id;
export const appServerId = app.id;
