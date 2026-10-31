// Total recurring hosting must stay strictly below this, including provider
// backups, tax and the temporary overlap with the legacy server (owner decision
// 2026-10-29).
export const MONTHLY_CEILING_CENTS = 8000;

// Default plan: a small Headscale/DERP relay plus one heavier application host,
// both x86_64 shared-vCPU EU types. Sizes, locations and backup flags are
// overridable per stack, but every override still passes the budget gate.
export const DEFAULT_PLAN = {
  relayServerType: "cx23",
  appServerType: "cx43",
  relayLocation: "hel1",
  appLocation: "nbg1",
  relayProviderBackups: true,
  appProviderBackups: true,
} as const;

export const EU_LOCATIONS = ["fsn1", "nbg1", "hel1"] as const;

// x86_64 shared-vCPU types. Arm (CAX) is excluded because the NixOS disk and
// boot profile is only tested for x86_64. cpx12 has no published USD list price
// in the repo, so its cost always comes from the operator's account quote.
export const SERVER_TYPES = ["cx23", "cx33", "cx43", "cx53", "cpx12", "cpx22", "cpx32", "cpx42"] as const;

// Public EU list prices in USD cents, excluding IPv4 and tax, checked 2026-10-31
// (docs/access-relay.md cites the sources). They are references for tests and
// documentation only; the gate uses the operator's dated account quote.
export const LIST_PRICE_CENTS: Readonly<Record<string, number>> = {
  cx23: 649,
  cx33: 999,
  cx43: 1849,
  cx53: 3499,
  cpx22: 2299,
  cpx32: 4199,
  cpx42: 8199,
};

export type MonthlyQuote = {
  date: string;
  relayServerCents: number;
  appServerCents: number;
  relayProviderBackups: boolean;
  appProviderBackups: boolean;
  // Per primary IPv4 address; one per server.
  ipv4Cents: number;
  offsiteBackupCents: number;
  // Legacy server (and its IPv4) that stays billable during migration.
  existingServerCents: number;
  otherCents: number;
  taxBasisPoints: number;
};

export function serverType(role: string, value: string): string {
  if (!(SERVER_TYPES as readonly string[]).includes(value)) {
    throw new Error(`${role} must be one of the x86_64 shared-vCPU types ${SERVER_TYPES.join(", ")}`);
  }
  return value;
}

export function euLocation(role: string, value: string): string {
  if (!(EU_LOCATIONS as readonly string[]).includes(value)) {
    throw new Error(`${role} must be one of ${EU_LOCATIONS.join(", ")}`);
  }
  return value;
}

export function usdCents(value: string, field: string): number {
  if (!/^(0|[1-9][0-9]*)(\.[0-9]{1,2})?$/.test(value)) {
    throw new Error(`${field} must be a nonnegative USD amount with at most two decimals`);
  }
  const [dollars, cents = ""] = value.split(".");
  const result = Number(dollars) * 100 + Number(cents.padEnd(2, "0"));
  if (!Number.isSafeInteger(result)) throw new Error(`${field} exceeds the supported range`);
  return result;
}

export function percentBasisPoints(value: string): number {
  if (!/^(0|[1-9][0-9]*)(\.[0-9]{1,2})?$/.test(value)) {
    throw new Error("taxPercent must be a nonnegative percentage with at most two decimals");
  }
  const [whole, fractional = ""] = value.split(".");
  const result = Number(whole) * 100 + Number(fractional.padEnd(2, "0"));
  if (!Number.isSafeInteger(result) || result > 10000) {
    throw new Error("taxPercent must be between 0 and 100");
  }
  return result;
}

export type QuoteAssessment = {
  providerBackupCents: number;
  // Steady state after the legacy server is retired.
  steadyCents: number;
  // Worst month: steady state plus the legacy server. Every overlap month is
  // gated at this figure, so a longer overlap cannot silently break the ceiling.
  overlapCents: number;
};

export function assessQuote(quote: MonthlyQuote, now = new Date()): QuoteAssessment {
  const date = new Date(`${quote.date}T00:00:00Z`);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(quote.date) || Number.isNaN(date.getTime()) || date.toISOString().slice(0, 10) !== quote.date) {
    throw new Error("quoteDate must be a real ISO calendar date");
  }
  const todayUtc = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const ageDays = (todayUtc - date.getTime()) / 86400000;
  if (ageDays < 0 || ageDays > 7) {
    throw new Error("the itemized quote must be dated within the past seven calendar days");
  }
  for (const [field, value] of Object.entries(quote)) {
    if (field === "date" || typeof value === "boolean") continue;
    if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
      throw new Error(`${field} must be a nonnegative integer`);
    }
  }
  if (quote.relayServerCents === 0 || quote.appServerCents === 0 || quote.ipv4Cents === 0 ||
    quote.offsiteBackupCents === 0 || quote.existingServerCents === 0) {
    throw new Error("server, IPv4, off-site backup and existing-server amounts must each be positive");
  }
  // Hetzner charges 20% of the server price for image backups.
  const providerBackupCents =
    (quote.relayProviderBackups ? Math.ceil(quote.relayServerCents * 0.2) : 0) +
    (quote.appProviderBackups ? Math.ceil(quote.appServerCents * 0.2) : 0);
  const steadyBase = quote.relayServerCents + quote.appServerCents + 2 * quote.ipv4Cents +
    providerBackupCents + quote.offsiteBackupCents + quote.otherCents;
  const withTax = (base: number) => base + Math.ceil(base * quote.taxBasisPoints / 10000);
  const steadyCents = withTax(steadyBase);
  const overlapCents = withTax(steadyBase + quote.existingServerCents);
  if (overlapCents >= MONTHLY_CEILING_CENTS) {
    throw new Error(
      `quoted overlap-month total $${(overlapCents / 100).toFixed(2)} is not strictly below the ` +
      `$${(MONTHLY_CEILING_CENTS / 100).toFixed(2)} ceiling`);
  }
  return { providerBackupCents, steadyCents, overlapCents };
}
