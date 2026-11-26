// @vitest-environment node
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// Parsed from tokens.css itself, so a changed value is checked as shipped.
const css = readFileSync(new URL("./tokens.css", import.meta.url), "utf8");
const token = (name: string) => {
  const value = new RegExp(`--hc-color-${name}:\\s*(#[0-9a-f]{6});`, "i").exec(css)?.[1];
  if (!value) throw new Error(`no hex token --hc-color-${name}`);
  return value;
};

/** WCAG 2.x relative luminance and contrast ratio. */
const luminance = (hex: string) => {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
};
const contrast = (a: string, b: string) => {
  const [hi, lo] = [luminance(token(a)), luminance(token(b))].sort((x, y) => y - x);
  return (hi! + 0.05) / (lo! + 0.05);
};

describe("token contrast (WCAG AA)", () => {
  it.each([
    ["foreground", "background"],
    ["muted-foreground", "background"],
    ["foreground", "muted"],
    ["muted-foreground", "muted"],
    ["accent", "background"],
    ["destructive", "background"],
    ["destructive", "muted"],
    ["destructive-foreground", "destructive"],
  ])("text %s on %s is at least 4.5:1", (fg, bg) => {
    expect(contrast(fg, bg)).toBeGreaterThanOrEqual(4.5);
  });

  // The website's copper mapping (tokens.css header): large text and UI parts only.
  it.each([
    ["primary", "background"],
    ["primary-foreground", "primary"],
    ["ring", "background"],
    ["border", "background"],
  ])("%s on %s is at least 3:1", (fg, bg) => {
    expect(contrast(fg, bg)).toBeGreaterThanOrEqual(3);
  });

  it("states the known copper gap: under 4.5:1, so primary is not used for body-size text", () => {
    expect(contrast("primary", "background")).toBeCloseTo(4.21, 2);
    expect(contrast("primary-foreground", "primary")).toBeCloseTo(4.21, 2);
  });

  it("uses the website palette", () => {
    expect([
      token("background"),
      token("foreground"),
      token("primary"),
      token("border"),
      token("accent"),
    ]).toEqual(["#221f1c", "#f3ede2", "#b8702f", "#8c847b", "#d7cbb9"]);
  });
});
