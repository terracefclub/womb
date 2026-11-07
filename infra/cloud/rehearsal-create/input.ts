import { constants, closeSync, fstatSync, openSync, readSync } from "node:fs";
import { isIP } from "node:net";
import { isAbsolute } from "node:path";
import { euLocation, serverType } from "../budget";

type Foundation = {
  run: string;
  phase: "foundation";
  sshPublicKey: string;
  sshCidrs: string[];
};

type Cohort = {
  run: string;
  phase: "original" | "replacement";
  sshKeyId: number;
  firewallId: number;
  image: "debian-13";
  location: string;
  appType: string;
  relayType: string;
};

export type RehearsalInput = Foundation | Cohort;

const MAX_INPUT = 4096;
const ED25519_PREFIX = Buffer.from("0000000b7373682d6564323535313900000020", "hex");

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

export function parseInput(value: unknown, stack: string): RehearsalInput {
  if (stack !== "foundation" && stack !== "original" && stack !== "replacement") {
    throw new Error("unsupported rehearsal stack");
  }
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("invalid rehearsal input");
  }
  const record = value as Record<string, unknown>;
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

  exactKeys(record, ["run", "phase", "sshKeyId", "firewallId", "image", "location", "appType", "relayType"]);
  const { image, location, appType, relayType } = record;
  if (image !== "debian-13" || typeof location !== "string"
    || typeof appType !== "string" || typeof relayType !== "string") {
    throw new Error("invalid rehearsal image, location or server types");
  }
  return {
    run,
    phase: stack,
    image,
    location: euLocation("rehearsal location", location),
    appType: serverType("rehearsal application", appType),
    relayType: serverType("rehearsal relay", relayType),
    sshKeyId: inputId(record.sshKeyId),
    firewallId: inputId(record.firewallId),
  };
}

export function readInput(path: string | undefined, stack: string): RehearsalInput {
  if (!path || !isAbsolute(path)) throw new Error("invalid rehearsal input path");
  const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const first = fstatSync(fd, { bigint: true });
    if (!first.isFile() || first.uid !== BigInt(process.geteuid!())
      || (first.mode & 0o7777n) !== 0o600n || first.nlink !== 1n
      || first.size < 1n || first.size > BigInt(MAX_INPUT)) {
      throw new Error("invalid private rehearsal input");
    }
    const bytes = Buffer.alloc(MAX_INPUT + 1);
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
    const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes.subarray(0, count));
    return parseInput(JSON.parse(text), stack);
  } finally {
    closeSync(fd);
  }
}
