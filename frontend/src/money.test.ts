import { describe, expect, it } from "vitest";

import {
  ZERO_MONEY,
  asMoney,
  asQuantity,
  compareDecimal,
  formatMoney,
  formatQuantity,
  isNegative,
  isZero,
  signOf,
  toChartNumber,
} from "./money";

describe("formatMoney", () => {
  it("formats without going through a float", () => {
    expect(formatMoney("150.0000")).toBe("$150.00");
    expect(formatMoney("1234.5600")).toBe("$1,234.56");
    expect(formatMoney("0.0000")).toBe("$0.00");
    expect(formatMoney("-42.5000")).toBe("-$42.50");
  });

  it("rounds the third decimal half-up, on the digits", () => {
    expect(formatMoney("1.2350")).toBe("$1.24");
    expect(formatMoney("1.2340")).toBe("$1.23");
  });

  it("keeps a value a double cannot hold", () => {
    // 0.1 + 0.2 is the canonical float failure. The point of strings is that this stays exact all
    // the way to the screen.
    expect(formatMoney("0.1000")).toBe("$0.10");
    expect(formatMoney("9007199254740993.0000")).toBe("$9,007,199,254,740,993.00");
  });
});

describe("formatQuantity", () => {
  it("strips trailing zeros without losing precision", () => {
    expect(formatQuantity("6.50000000")).toBe("6.5");
    expect(formatQuantity("100.00000000")).toBe("100");
    expect(formatQuantity("0.00000001")).toBe("0.00000001");
  });
});

describe("signOf", () => {
  it("reads the sign off the digits", () => {
    expect(signOf("0.0000")).toBe(0);
    expect(signOf("-0.0000")).toBe(0);
    expect(signOf("0.0001")).toBe(1);
    expect(signOf("-0.0001")).toBe(-1);
  });

  it("treats a negative zero as zero, not as a debit", () => {
    expect(isNegative("-0.0000")).toBe(false);
  });
});

describe("the boundary helpers", () => {
  it("brands without changing the value", () => {
    // The brands are compile-time only; at runtime these must be the identity, or every equality
    // check against a literal in this codebase would be quietly wrong.
    expect(asMoney("1.0000")).toBe("1.0000");
    expect(asQuantity("1.00000000")).toBe("1.00000000");
    expect(ZERO_MONEY).toBe("0.0000");
    expect(isZero(ZERO_MONEY)).toBe(true);
  });

  it("converts to a float only at the charting boundary", () => {
    // The single sanctioned lossy conversion. Asserted so it is deliberate rather than incidental.
    expect(toChartNumber("153.8500")).toBeCloseTo(153.85, 4);
  });
});

describe("compareDecimal", () => {
  it("compares by magnitude, not lexicographically", () => {
    // The bug this exists to prevent: "10.0000" < "9.9999" as strings.
    expect(compareDecimal("10.0000", "9.9999")).toBeGreaterThan(0);
    expect(compareDecimal("-10.0000", "-9.9999")).toBeLessThan(0);
    expect(compareDecimal("5.0000", "5.0000")).toBe(0);
  });
});
