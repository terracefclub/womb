export type MonthlyQuote = {
  date: string;
  serverCents: number;
  ipv4Cents: number;
  offsiteBackupCents: number;
  otherCents: number;
  taxBasisPoints: number;
};

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

export function assessQuote(quote: MonthlyQuote, now = new Date()): {
  providerBackupCents: number;
  totalCents: number;
} {
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
    if (field === "date") continue;
    if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
      throw new Error(`${field} must be a nonnegative integer`);
    }
  }
  if (quote.serverCents === 0 || quote.ipv4Cents === 0 || quote.offsiteBackupCents === 0) {
    throw new Error("server, IPv4, and off-site backup allowances must each be positive");
  }
  const providerBackupCents = Math.ceil(quote.serverCents * 0.2);
  const subtotal = quote.serverCents + quote.ipv4Cents + providerBackupCents + quote.offsiteBackupCents + quote.otherCents;
  const totalCents = subtotal + Math.ceil(subtotal * quote.taxBasisPoints / 10000);
  if (totalCents > 2000) {
    throw new Error(`quoted recurring total $${(totalCents / 100).toFixed(2)} exceeds the $20.00 cap`);
  }
  return { providerBackupCents, totalCents };
}
