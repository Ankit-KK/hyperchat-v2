import type { Schemas } from "@hyperchat/api-client";
import { notFound } from "next/navigation";
import { cache } from "react";
import { serverApi } from "../../../lib/api";
import { isMarketingName } from "../../../lib/marketing";

export type Creator = Schemas["PublicStreamer"];
export type Pricing = Schemas["Pricing"];
export type Goal = Schemas["Goal"];

/** The canonical host (owner decision R3): creator pages are always canonical on the apex. */
export const SITE_ORIGIN = "https://hyperchat.space";

// The API's slug rule (backend/app/public/service.py): anything else cannot be a creator, so the API is not asked.
const SLUG = /^[a-z0-9][a-z0-9_-]{0,63}$/;

/** Thrown when the API cannot answer (network, timeout, 5xx): the page shows "unavailable", never "not found". */
export class CreatorUnavailable extends Error {}

/**
 * One API call per request, shared by generateMetadata, the page and the Open Graph image. The API serves active
 * creators only and answers 404 for every other one, so it alone decides what exists.
 */
export const loadCreator = cache(async (slug: string): Promise<Creator> => {
  // A marketing name is forwarded to the website before it gets here; never treat one as a creator either way.
  if (!SLUG.test(slug) || isMarketingName(slug)) notFound();
  const result = await serverApi().get("/api/v1/public/streamers/{slug}", { params: { slug } });
  if (result.ok) return result.data;
  if (result.error.kind === "http" && result.error.status === 404) notFound();
  throw new CreatorUnavailable(`creator page unavailable: ${result.error.error}`);
});

/** Pricing and goal are extras: when either call fails the page still renders, without that section. */
export async function loadExtras(slug: string): Promise<{ pricing: Pricing | null; goal: Goal | null }> {
  const api = serverApi();
  const [pricing, goal] = await Promise.all([
    api.get("/api/v1/public/streamers/{slug}/pricing", { params: { slug } }),
    api.get("/api/v1/public/streamers/{slug}/goal", { params: { slug } }),
  ]);
  return { pricing: pricing.ok ? pricing.data : null, goal: goal.ok && goal.data.active ? goal.data : null };
}

/**
 * An image the page may load: absolute https only. A legacy `/assets/...` path (the old site's bundle, now the
 * marketing website's) or anything else gets the placeholder instead.
 */
export function imageSrc(url: string | null | undefined): string | null {
  return url && /^https:\/\/[^/]/i.test(url) ? url : null;
}

/** The creator's colour for decorative accents only (rings, bars), never text; copper when unset or malformed. */
export function accentColor(color: string | null | undefined): string {
  return color && /^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i.test(color) ? color : "var(--hc-color-primary)";
}

export function formatAmount(amount: number, currency: string): string {
  try {
    return new Intl.NumberFormat("en-IN", { style: "currency", currency, maximumFractionDigits: 2 }).format(
      amount,
    );
  } catch {
    return `${amount} ${currency}`; // an unknown currency code
  }
}

export function description(name: string): string {
  return `Support ${name} with live interactions and stream alerts on HyperChat.`;
}
