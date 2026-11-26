// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Creator, Goal, Pricing } from "./creator";

// The typed client is replaced per test: each API path answers from `answers`.
type Answer = { ok: true; status: number; data: unknown } | { ok: false; error: unknown };
const answers = new Map<string, Answer>();
vi.mock("../../../lib/api", () => ({
  serverApi: () => ({
    get: async (path: string) =>
      answers.get(path) ?? { ok: false, error: { kind: "network", error: "down" } },
  }),
}));

const { default: CreatorPage, generateMetadata } = await import("./page");
const { CreatorView } = await import("./creator-view");
const { accentColor, imageSrc, ogImageSrc } = await import("./creator");

const creator: Creator = {
  slug: "tsukii",
  name: "Tsukii",
  brand_color: "#4fb3a9",
  brand_logo_url: "https://assets.example/creator-assets/u/profile/a.webp",
  background_url: "https://assets.example/creator-assets/u/background/b.webp",
  tts_enabled: true,
  media_upload_enabled: false,
  media_max_file_size_mb: null,
  soundboard_enabled: true,
};
const pricing: Pricing = {
  currency: "INR",
  min_text: 40,
  min_tts: 40,
  min_voice: 150,
  min_hypersound: 100,
  min_media: 200,
  tts_enabled: true,
  message_char_tiers: null,
  voice_duration_tiers: [],
};
const goal: Goal = {
  active: true,
  name: "New microphone",
  target_amount_inr: 10000,
  progress_inr: 2500,
  brand_color: "#4fb3a9",
  goal_overlay_id: null,
};

const ok = (data: unknown): Answer => ({ ok: true, status: 200, data });
const params = (slug = "tsukii") => ({ params: Promise.resolve({ slug }) });
function serve(c: Creator | Answer, p: Pricing | null = pricing, g: Goal | null = goal) {
  answers.clear();
  answers.set("/api/v1/public/streamers/{slug}", "ok" in c ? c : ok(c));
  if (p) answers.set("/api/v1/public/streamers/{slug}/pricing", ok(p));
  answers.set("/api/v1/public/streamers/{slug}/goal", ok(g ?? { ...goal, active: false }));
}

/** axe on the rendered page; jsdom cannot compute colour contrast, which tokens.test.ts covers instead. */
async function expectAccessible(container: HTMLElement) {
  const { violations } = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
  expect(violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
}

const statusOf = async (promise: Promise<unknown>) => {
  try {
    await promise;
    return "rendered";
  } catch (error) {
    return (error as { digest?: string }).digest ?? (error as Error).constructor.name;
  }
};

afterEach(cleanup);

describe("creator page", () => {
  it("has an axe check that can fail", async () => {
    const { container } = render(<button type="button" />); // a button without a name
    const { violations } = await axe.run(container);
    expect(violations.map((v) => v.id)).toContain("button-name");
  });

  it("renders an active creator: images with dimensions, name, goal, pricing and a disabled call to action", async () => {
    serve(creator);
    const { container } = render(await CreatorPage(params()));
    expect(screen.getByRole("heading", { level: 1, name: "Tsukii" })).toBeTruthy();
    const images = container.querySelectorAll("img");
    expect(images).toHaveLength(2);
    for (const img of images) {
      expect(img.getAttribute("width")).toBeTruthy();
      expect(img.getAttribute("height")).toBeTruthy();
    }
    expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("25");
    const prices = within(screen.getByRole("region", { name: "Interactions start at" }));
    expect(prices.getByText("Text-to-speech")).toBeTruthy();
    expect(prices.getByText("Sound")).toBeTruthy();
    expect(prices.queryByText("Image or GIF")).toBeNull(); // media uploads are off for this creator
    const cta = screen.getByRole("button", { name: "Send an interaction" }) as HTMLButtonElement;
    expect(cta.disabled).toBe(true);
    expect(cta.getAttribute("aria-describedby")).toBe("send-soon");
    expect(container.textContent).toMatch(/coming soon/i);
    expect(container.textContent).not.toMatch(/donat/i);
    await expectAccessible(container);
  });

  it("emits Organization JSON-LD for Streamheart Private Limited, with < escaped", async () => {
    serve({ ...creator, name: "</script><b>x" });
    const { container } = render(await CreatorPage(params()));
    const ld = container.querySelector('script[type="application/ld+json"]')!.innerHTML;
    expect(ld).not.toContain("</script>");
    const graph = JSON.parse(ld)["@graph"];
    expect(graph[0]).toMatchObject({ "@type": "Organization", legalName: "Streamheart Private Limited" });
    expect(graph[1].url).toBe("https://hyperchat.space/tsukii");
  });

  it("shows placeholders when images are missing", async () => {
    serve({ ...creator, brand_logo_url: null, background_url: null });
    const { container } = render(await CreatorPage(params()));
    expect(container.querySelectorAll("img")).toHaveLength(0);
    expect(screen.getByTestId("background-placeholder")).toBeTruthy();
    expect(screen.getByTestId("profile-placeholder").textContent).toBe("T");
    await expectAccessible(container);
  });

  it("never loads a legacy /assets/... path (now the marketing website's)", async () => {
    serve({ ...creator, brand_logo_url: "/assets/tsukii-logo.png", background_url: "/assets/tsukii-bg.jpg" });
    const { container } = render(await CreatorPage(params()));
    expect(container.querySelectorAll("img")).toHaveLength(0);
    expect(screen.getByTestId("background-placeholder")).toBeTruthy();
  });

  it("hides the goal when none is active, and pricing when its call fails", async () => {
    serve(creator, null, null);
    const { container } = render(await CreatorPage(params()));
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(screen.queryByRole("region", { name: "Interactions start at" })).toBeNull();
    expect(screen.getByRole("button", { name: "Send an interaction" })).toBeTruthy();
    await expectAccessible(container);
  });

  it("is a 404 when the API says so (unknown or not active)", async () => {
    serve({ ok: false, error: { kind: "http", status: 404, error: "Streamer not found", code: null } });
    expect(await statusOf(CreatorPage(params()))).toMatch(/;404$/);
    expect(await statusOf(generateMetadata(params()))).toMatch(/;404$/);
  });

  it("is a 404 for marketing names and malformed slugs, without asking the API", async () => {
    answers.clear();
    for (const slug of ["features", "creators", "assets", "Tsukii", "a.b"]) {
      expect(await statusOf(CreatorPage(params(slug)))).toMatch(/;404$/);
    }
  });

  it("is 'unavailable', never a 404, when the API is down; its metadata is noindex", async () => {
    answers.clear(); // every call answers { kind: "network" }
    expect(await statusOf(CreatorPage(params()))).toBe("CreatorUnavailable");
    expect((await generateMetadata(params())).robots).toEqual({ index: false, follow: false });
    serve({ ok: false, error: { kind: "http", status: 502, error: "Bad gateway", code: null } });
    expect(await statusOf(CreatorPage(params()))).toBe("CreatorUnavailable");
  });
});

describe("metadata", () => {
  it("templates title and description from the name, canonical on the apex", async () => {
    serve(creator);
    const meta = await generateMetadata(params());
    expect(meta.title).toEqual({ absolute: "Tsukii — Support on HyperChat" });
    expect(meta.description).toBe("Support Tsukii with live interactions and stream alerts on HyperChat.");
    expect(meta.alternates?.canonical).toBe("https://hyperchat.space/tsukii");
    expect(String(meta.metadataBase)).toBe("https://hyperchat.space/"); // og:image is on the apex too
    expect(meta.openGraph).toMatchObject({ url: "https://hyperchat.space/tsukii", siteName: "HyperChat" });
    expect(meta.twitter).toMatchObject({ card: "summary_large_image" });
    expect(meta.robots).toBeUndefined(); // active creators may be indexed
    expect(JSON.stringify(meta)).not.toMatch(/donat/i);
  });
});

describe("helpers", () => {
  it("accepts https image URLs only", () => {
    expect(imageSrc("https://a.example/x.webp")).toBe("https://a.example/x.webp");
    for (const bad of [
      null,
      "",
      "/assets/x.png",
      "http://a.example/x.png",
      "javascript:alert(1)",
      "https:///x",
    ]) {
      expect(imageSrc(bad)).toBeNull();
    }
  });

  it("lets the share card fetch only pictures under the media base", () => {
    const base = "https://media.example";
    expect(ogImageSrc(`${base}/creator-assets/u/profile/a.webp`, base)).toBe(
      `${base}/creator-assets/u/profile/a.webp`,
    );
    expect(ogImageSrc(`${base}/x.webp`, `${base}/`)).toBe(`${base}/x.webp`);
    for (const bad of [
      "https://other.example/x.webp",
      "https://media.example.evil.test/x.webp",
      "https://media.example@evil.test/x.webp",
      "http://media.example/x.webp",
      null,
    ]) {
      expect(ogImageSrc(bad, base)).toBeNull();
    }
    expect(ogImageSrc(`${base}/x.webp`, undefined)).toBeNull(); // unset: no fetch at all
    expect(ogImageSrc(`${base}/x.webp`, "http://media.example")).toBeNull();
  });

  it("uses the creator colour only when it is a hex colour near the palette, else copper", () => {
    expect(accentColor("#ABC")).toBe("#ABC"); // a near-grey
    for (const warm of ["#B8702F", "#d94f30", "#e0a030", "#8b4513", "#ffff00", "#dc143c"]) {
      expect(accentColor(warm)).toBe(warm); // hues 28, 11, 38, 25, 60 (yellow) and 348 (crimson)
    }
    // purple, violet, blue, teal, green, magenta; chartreuse (79°) and hot pink (336°) are just outside the window
    for (const far of ["#8b5cf6", "#7c3aed", "#2563eb", "#4fb3a9", "#22c55e", "#f0f", "#b0ff00", "#ff0066"]) {
      expect(accentColor(far)).toBe("var(--hc-color-primary)");
    }
    expect(accentColor("red; background:url(x)")).toBe("var(--hc-color-primary)");
    expect(accentColor(null)).toBe("var(--hc-color-primary)");
  });

  it("renders CreatorView alone from a fixture", () => {
    render(<CreatorView creator={creator} pricing={null} goal={{ ...goal, target_amount_inr: null }} />);
    expect(screen.queryByRole("progressbar")).toBeNull(); // no target: amount raised only
    expect(screen.getByText(/raised/)).toBeTruthy();
  });
});
