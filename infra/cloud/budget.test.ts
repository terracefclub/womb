import { expect, test } from "bun:test";
import {
  DEFAULT_PLAN,
  LIST_PRICE_CENTS,
  MONTHLY_CEILING_CENTS,
  assessQuote,
  euLocation,
  percentBasisPoints,
  serverType,
  usdCents,
} from "./budget";

const now = new Date("2026-10-31T12:00:00Z");

// Public EU list prices checked 2026-10-31 for the default plan. Existing
// legacy CPX11 is the pre-2026-06-15 US price ($6.99) plus a $0.60 IPv4.
const quote = {
  date: "2026-10-31",
  relayServerCents: LIST_PRICE_CENTS[DEFAULT_PLAN.relayServerType],
  appServerCents: LIST_PRICE_CENTS[DEFAULT_PLAN.appServerType],
  relayProviderBackups: DEFAULT_PLAN.relayProviderBackups,
  appProviderBackups: DEFAULT_PLAN.appProviderBackups,
  ipv4Cents: 60,
  offsiteBackupCents: 500,
  existingServerCents: 759,
  otherCents: 100,
  taxBasisPoints: 0,
};

test("default plan prices match the documented arithmetic", () => {
  expect(DEFAULT_PLAN).toMatchObject({ relayServerType: "cx23", appServerType: "cx43" });
  expect(quote.relayServerCents).toBe(649);
  expect(quote.appServerCents).toBe(1849);
  // 20% image backups: relay $1.30 + app $3.70.
  expect(assessQuote(quote, now)).toEqual({
    providerBackupCents: 500,
    steadyCents: 3718,
    overlapCents: 4477,
  });
});

test("default plan stays strictly below $80 with 19% VAT and a repriced legacy server", () => {
  const vat = assessQuote({ ...quote, taxBasisPoints: 1900 }, now);
  expect(vat).toEqual({ providerBackupCents: 500, steadyCents: 4425, overlapCents: 5328 });
  // If the legacy CPX11 were repriced to the current US list price ($20.49 + $0.60 IPv4).
  const repriced = assessQuote({ ...quote, existingServerCents: 2109, taxBasisPoints: 1900 }, now);
  expect(repriced.overlapCents).toBe(6935);
  expect(repriced.overlapCents).toBeLessThan(MONTHLY_CEILING_CENTS);
});

test("provider backups can be disabled per host", () => {
  const none = assessQuote({ ...quote, relayProviderBackups: false, appProviderBackups: false }, now);
  expect(none.providerBackupCents).toBe(0);
  expect(none.overlapCents).toBe(3977);
});

test("CPX fallback fits only without VAT and with app-only provider backups", () => {
  const fallback = {
    ...quote,
    relayServerCents: 1370, // derived allowance for cpx12 (no USD list price published); confirm in the account quote
    appServerCents: LIST_PRICE_CENTS.cpx32,
    relayProviderBackups: false,
    appProviderBackups: true,
  };
  expect(assessQuote(fallback, now).overlapCents).toBe(7888);
  expect(() => assessQuote({ ...fallback, taxBasisPoints: 1900 }, now)).toThrow("not strictly below");
});

test("the ceiling is strict: exactly $80.00 fails and $79.99 passes", () => {
  // Steady month is 3718 cents; add the legacy server to land on the boundary.
  const at = (existingServerCents: number) => assessQuote({ ...quote, existingServerCents }, now);
  expect(() => at(8000 - 3718)).toThrow("not strictly below");
  expect(at(8000 - 3718 - 1).overlapCents).toBe(7999);
});

test("over-ceiling and stale quotes fail closed", () => {
  expect(() => assessQuote({ ...quote, appServerCents: LIST_PRICE_CENTS.cpx42 }, now)).toThrow("not strictly below");
  expect(assessQuote(quote, new Date("2026-11-07T23:59:59Z")).overlapCents).toBe(4477);
  expect(() => assessQuote(quote, new Date("2026-11-08T00:00:00Z"))).toThrow("within the past");
  expect(() => assessQuote({ ...quote, date: "2026-11-05" }, now)).toThrow("within the past");
});

test("zero or malformed amounts fail closed", () => {
  expect(() => assessQuote({ ...quote, existingServerCents: 0 }, now)).toThrow("positive");
  expect(() => assessQuote({ ...quote, offsiteBackupCents: 0 }, now)).toThrow("positive");
  expect(() => assessQuote({ ...quote, otherCents: -1 }, now)).toThrow("nonnegative");
  expect(usdCents("9.99", "server")).toBe(999);
  expect(() => usdCents("9.999", "server")).toThrow();
  expect(percentBasisPoints("8.25")).toBe(825);
  expect(() => percentBasisPoints("101")).toThrow();
});

test("only tested x86_64 EU types and locations are accepted", () => {
  expect(serverType("relay", "cx23")).toBe("cx23");
  expect(() => serverType("relay", "cax11")).toThrow();
  expect(() => serverType("app", "cpx31")).toThrow();
  expect(euLocation("app", "nbg1")).toBe("nbg1");
  expect(() => euLocation("app", "ash")).toThrow();
});
