import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { cache } from "react";
import { serverApi } from "../../../lib/api";

// The API's slug rule (backend/app/authz/access.py): anything else cannot be a creator, so the API is not asked.
const SLUG = /^[a-z0-9][a-z0-9_-]{0,63}$/;

type Props = { params: Promise<{ slug: string }> };

/** One API call per request, shared by generateMetadata and the page. The API serves only active creators. */
const loadCreator = cache(async (slug: string) => {
  if (!SLUG.test(slug)) notFound();
  const result = await serverApi().get("/api/v1/public/streamers/{slug}", { params: { slug } });
  if (result.ok) return result.data;
  if (result.error.kind === "http" && result.error.status === 404) notFound();
  throw new Error(`creator page unavailable: ${result.error.error}`);
});

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const creator = await loadCreator((await params).slug);
  return { title: { absolute: `${creator.name} — Support on HyperChat` } };
}

/** Phase 2 placeholder: name, colour and images only. The full creator page and donation form are Phase 4 and 7. */
export default async function CreatorPage({ params }: Props) {
  const creator = await loadCreator((await params).slug);
  return (
    <main className="mx-auto max-w-5xl px-4 py-12">
      {creator.background_url && (
        // eslint-disable-next-line @next/next/no-img-element -- remote creator images; next/image config is Phase 4
        <img src={creator.background_url} alt="" className="aspect-video w-full rounded-lg object-cover" />
      )}
      <div className="mt-6 flex items-center gap-4">
        {creator.brand_logo_url && (
          // eslint-disable-next-line @next/next/no-img-element -- as above
          <img src={creator.brand_logo_url} alt="" className="size-16 rounded-full object-cover" />
        )}
        <h1
          className="text-3xl font-semibold"
          style={creator.brand_color ? { color: creator.brand_color } : undefined}
        >
          {creator.name}
        </h1>
      </div>
    </main>
  );
}
