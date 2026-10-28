import * as fs from "node:fs";
import * as path from "node:path";
import { createHash } from "node:crypto";
import { isIP } from "node:net";
import * as hcloud from "@pulumi/hcloud";
import * as pulumi from "@pulumi/pulumi";
import { assessQuote, percentBasisPoints, usdCents } from "./budget";

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

const location = config.require("location");
if (!["fsn1", "nbg1", "hel1"].includes(location)) {
  throw new Error("bootstrap location must be an EU x86_64 CX location");
}
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
  serverCents: usdCents(config.require("serverMonthlyUsd"), "serverMonthlyUsd"),
  ipv4Cents: usdCents(config.require("ipv4MonthlyUsd"), "ipv4MonthlyUsd"),
  offsiteBackupCents: usdCents(config.require("offsiteBackupMonthlyUsd"), "offsiteBackupMonthlyUsd"),
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
const server = new hcloud.Server("bootstrap-server", {
  name: "terrace-bootstrap-1",
  location,
  image: bootstrapImage,
  serverType: "cx33",
  backups: true,
  deleteProtection: true,
  rebuildProtection: true,
  firewallIds: [firewall.id.apply(Number)],
  sshKeys: [sshKey.name],
  publicNets: [{ ipv4Enabled: true, ipv6Enabled: false }],
  labels,
}, { protect: true });

export const quotedMonthlyUsd = (quote.totalCents / 100).toFixed(2);
export const serverId = server.id;
