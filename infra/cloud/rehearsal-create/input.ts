import { createHash } from "node:crypto";
import { constants, closeSync, fstatSync, lstatSync, openSync, readSync, realpathSync } from "node:fs";
import { isIP } from "node:net";
import { basename, dirname, isAbsolute, join } from "node:path";
import { euLocation, serverType } from "../budget";
import { renderBootstrapCloudConfig } from "./bootstrap";

type Foundation = {
  run: string;
  phase: "foundation";
  sshPublicKey: string;
  sshCidrs: string[];
};

type HostRole = "app" | "relay";
type BootstrapKey = { publicKey: string; privateKeySha256: string };
type BootstrapInput = {
  bootstrapHostKeys: Partial<Record<HostRole, BootstrapKey>>;
  bootstrapOperatorPublicKey: string;
};

type CohortBase = {
  run: string;
  phase: "original" | "replacement";
  sshKeyId: number;
  firewallId: number;
  image: "debian-13";
  location: string;
  appType: string;
  relayType: string;
  probeOnly?: true;
};

type Cohort = CohortBase & (BootstrapInput | { bootstrapHostKeys?: never; bootstrapOperatorPublicKey?: never });

export type RehearsalInput = Foundation | Cohort;

const MAX_INPUT = 4096;
const MAX_PRIVATE_KEY = 2048;
const ED25519_PREFIX = Buffer.from("0000000b7373682d6564323535313900000020", "hex");

export function cohortRoles(probeOnly?: true): readonly HostRole[] {
  return probeOnly ? ["relay"] : ["app", "relay"];
}

function exactKeys(record: Record<string, unknown>, expected: string[]): void {
  const keys = Object.keys(record);
  if (keys.length !== expected.length || keys.some(key => !expected.includes(key))) {
    throw new Error("unexpected rehearsal input keys");
  }
}

function inputId(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 1) {
    throw new Error("invalid foundation resource ID");
  }
  return value;
}

function publicKey(value: unknown): string {
  if (typeof value !== "string") throw new Error("invalid rehearsal public key");
  const match = /^ssh-ed25519 ([A-Za-z0-9+/]{68})(?: [ -~]{1,120})?$/.exec(value.trimEnd());
  if (!match) throw new Error("invalid rehearsal public key");
  const blob = Buffer.from(match[1], "base64");
  if (blob.length !== 51 || !blob.subarray(0, ED25519_PREFIX.length).equals(ED25519_PREFIX)) {
    throw new Error("invalid rehearsal public key");
  }
  return "ssh-ed25519 " + match[1];
}

function recordOf(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("invalid rehearsal input");
  }
  return value as Record<string, unknown>;
}

function canonicalPublicKey(value: unknown): string {
  const key = publicKey(value);
  if (key !== value) throw new Error("bootstrap public key must be canonical");
  return key;
}

function parseBootstrap(record: Record<string, unknown>, roles: readonly HostRole[]): BootstrapInput {
  const operator = canonicalPublicKey(record.bootstrapOperatorPublicKey);
  const supplied = recordOf(record.bootstrapHostKeys);
  exactKeys(supplied, [...roles]);
  const hostKeys: BootstrapInput["bootstrapHostKeys"] = {};
  const keys = new Set([operator]);
  const hashes = new Set<string>();
  for (const role of roles) {
    const entry = recordOf(supplied[role]);
    exactKeys(entry, ["publicKey", "privateKeySha256"]);
    const key = canonicalPublicKey(entry.publicKey);
    const hash = entry.privateKeySha256;
    if (typeof hash !== "string" || hash.length !== 64 || !/^[a-f0-9]+$/.test(hash)
      || keys.has(key) || hashes.has(hash)) {
      throw new Error("invalid or reused bootstrap host key");
    }
    keys.add(key);
    hashes.add(hash);
    hostKeys[role] = { publicKey: key, privateKeySha256: hash };
  }
  return { bootstrapHostKeys: hostKeys, bootstrapOperatorPublicKey: operator };
}

export function parseInput(value: unknown, stack: string): RehearsalInput {
  if (stack !== "foundation" && stack !== "original" && stack !== "replacement") {
    throw new Error("unsupported rehearsal stack");
  }
  const record = recordOf(value);
  const { run, phase } = record;
  if (phase !== stack || typeof run !== "string" || run.length !== 32 || !/^[a-f0-9]+$/.test(run)) {
    throw new Error("invalid rehearsal identity");
  }

  if (stack === "foundation") {
    exactKeys(record, ["run", "phase", "sshPublicKey", "sshCidrs"]);
    const cidrs = record.sshCidrs;
    if (!Array.isArray(cidrs) || cidrs.length < 1 || cidrs.length > 8
      || cidrs.some(cidr => typeof cidr !== "string" || !cidr.endsWith("/32")
        || isIP(cidr.slice(0, -3)) !== 4)
      || new Set(cidrs).size !== cidrs.length) {
      throw new Error("rehearsal SSH sources must be unique IPv4 /32 CIDRs");
    }
    return { run, phase: stack, sshPublicKey: publicKey(record.sshPublicKey), sshCidrs: cidrs };
  }

  const probeOnly = Object.prototype.hasOwnProperty.call(record, "probeOnly");
  const expected = ["run", "phase", "sshKeyId", "firewallId", "image", "location", "appType", "relayType"];
  if (probeOnly) {
    if (stack !== "original" || record.probeOnly !== true) throw new Error("invalid console probe mode");
    expected.push("probeOnly");
  }
  const bootstrap = Object.prototype.hasOwnProperty.call(record, "bootstrapHostKeys")
    || Object.prototype.hasOwnProperty.call(record, "bootstrapOperatorPublicKey");
  if (bootstrap) expected.push("bootstrapHostKeys", "bootstrapOperatorPublicKey");
  exactKeys(record, expected);
  const { image, location, appType, relayType } = record;
  if (image !== "debian-13" || typeof location !== "string"
    || typeof appType !== "string" || typeof relayType !== "string") {
    throw new Error("invalid rehearsal image, location or server types");
  }
  const cohort: CohortBase = {
    run,
    phase: stack,
    image,
    location: euLocation("rehearsal location", location),
    appType: serverType("rehearsal application", appType),
    relayType: serverType("rehearsal relay", relayType),
    sshKeyId: inputId(record.sshKeyId),
    firewallId: inputId(record.firewallId),
    ...(probeOnly ? { probeOnly: true as const } : {}),
  };
  // Pulumi type-checks this program before it runs; keep each branch exact.
  return bootstrap ? { ...cohort, ...parseBootstrap(record, cohortRoles(probeOnly ? true : undefined)) } : cohort;
}

function readPrivateFile(path: string, limit: number): Buffer {
  const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const first = fstatSync(fd, { bigint: true });
    if (!first.isFile() || first.uid !== BigInt(process.geteuid!())
      || (first.mode & 0o7777n) !== 0o600n || first.nlink !== 1n
      || first.size < 1n || first.size > BigInt(limit)) {
      throw new Error("invalid private rehearsal input");
    }
    const bytes = Buffer.alloc(limit + 1);
    let count = 0;
    while (count < bytes.length) {
      const read = readSync(fd, bytes, count, bytes.length - count, null);
      if (read === 0) break;
      count += read;
    }
    const last = fstatSync(fd, { bigint: true });
    if (BigInt(count) !== first.size || last.mtimeNs !== first.mtimeNs || last.ctimeNs !== first.ctimeNs) {
      throw new Error("private rehearsal input changed");
    }
    return bytes.subarray(0, count);
  } finally {
    closeSync(fd);
  }
}

export function readInput(path: string | undefined, stack: string): RehearsalInput {
  if (!path || !isAbsolute(path)) throw new Error("invalid rehearsal input path");
  const text = new TextDecoder("utf-8", { fatal: true }).decode(readPrivateFile(path, MAX_INPUT));
  return parseInput(JSON.parse(text), stack);
}

export function readBootstrapUserData(path: string | undefined, input: Cohort): Partial<Record<HostRole, string>> {
  if (!input.bootstrapHostKeys) return {};
  if (!path || !isAbsolute(path) || basename(path) !== input.phase + "-input.json") {
    throw new Error("invalid bootstrap input path");
  }
  const directory = dirname(path);
  const facts = lstatSync(directory);
  if (!facts.isDirectory() || facts.uid !== process.geteuid!()
    || (facts.mode & 0o7777) !== 0o700 || realpathSync(directory) !== directory) {
    throw new Error("invalid private bootstrap directory");
  }
  const result: Partial<Record<HostRole, string>> = {};
  for (const role of cohortRoles(input.probeOnly)) {
    const key = input.bootstrapHostKeys[role];
    const bytes = readPrivateFile(join(directory, input.phase + "-" + role + "-bootstrap-ed25519"), MAX_PRIVATE_KEY);
    if (!key || createHash("sha256").update(bytes).digest("hex") !== key.privateKeySha256) {
      throw new Error("bootstrap private key digest mismatch");
    }
    const privateKey = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    if (!/^-----BEGIN OPENSSH PRIVATE KEY-----\n(?:[A-Za-z0-9+/=]+\n)+-----END OPENSSH PRIVATE KEY-----\n$/.test(privateKey)) {
      throw new Error("invalid bootstrap private key encoding");
    }
    result[role] = renderBootstrapCloudConfig(key.publicKey, privateKey, input.bootstrapOperatorPublicKey);
  }
  return result;
}
