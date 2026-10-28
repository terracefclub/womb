import { expect, test } from "bun:test";
import { assessQuote, percentBasisPoints, usdCents } from "./budget";

const quote = {
  date: "2026-10-28",
  serverCents: 999,
  ipv4Cents: 60,
  offsiteBackupCents: 300,
  otherCents: 0,
  taxBasisPoints: 1000,
};

test("itemized quote includes provider backup and tax under the cap", () => {
  expect(assessQuote(quote, new Date("2026-10-28T12:00:00Z"))).toEqual({
    providerBackupCents: 200,
    totalCents: 1715,
  });
});

test("over-cap and stale quotes fail closed", () => {
  expect(() => assessQuote({ ...quote, offsiteBackupCents: 600 }, new Date("2026-10-28T12:00:00Z"))).toThrow("exceeds");
  expect(assessQuote(quote, new Date("2026-11-04T23:59:59Z")).totalCents).toBe(1715);
  expect(() => assessQuote(quote, new Date("2026-11-05T00:00:00Z"))).toThrow("within the past");
  expect(() => assessQuote(quote, new Date("2026-11-10T12:00:00Z"))).toThrow("within the past");
});

test("malformed money and tax values fail closed", () => {
  expect(usdCents("9.99", "server")).toBe(999);
  expect(() => usdCents("9.999", "server")).toThrow();
  expect(percentBasisPoints("8.25")).toBe(825);
  expect(() => percentBasisPoints("101")).toThrow();
});
