import type { Metadata } from "next";
import { CreatorUnavailable, SITE_ORIGIN, description, loadCreator, loadExtras } from "./creator";
import { CreatorView } from "./creator-view";

type Props = { params: Promise<{ slug: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  let creator;
  try {
    creator = await loadCreator((await params).slug);
  } catch (error) {
    // The page itself throws again and renders the unavailable page; this keeps that response out of indexes.
    if (error instanceof CreatorUnavailable) return { robots: { index: false, follow: false } };
    throw error; // notFound()
  }
  const title = `${creator.name} — Support on HyperChat`;
  const url = `${SITE_ORIGIN}/${creator.slug}`;
  const text = description(creator.name);
  // og:image and twitter:image come from opengraph-image.tsx in this folder.
  return {
    metadataBase: new URL(SITE_ORIGIN), // the Open Graph image URL is on the canonical apex too
    title: { absolute: title },
    description: text,
    alternates: { canonical: url },
    openGraph: { type: "profile", siteName: "HyperChat", locale: "en_IN", url, title, description: text },
    twitter: { card: "summary_large_image", title, description: text },
  };
}

/** Organization JSON-LD as the website emits it (hyperchat-frontend scripts/build-seo.mjs), plus this page. */
function jsonLd(name: string, slug: string) {
  const url = `${SITE_ORIGIN}/${slug}`;
  return {
    "@context": "https://schema.org",
    "@graph": [
      {
        "@type": "Organization",
        "@id": `${SITE_ORIGIN}/#organization`,
        name: "HyperChat",
        legalName: "Streamheart Private Limited",
        url: `${SITE_ORIGIN}/`,
        email: "ankit@hyperchat.space",
      },
      {
        "@type": "ProfilePage",
        "@id": `${url}#webpage`,
        url,
        name: `${name} — Support on HyperChat`,
        description: description(name),
        inLanguage: "en-IN",
        publisher: { "@id": `${SITE_ORIGIN}/#organization` },
      },
    ],
  };
}

/** The public creator page (PDP §5, §16). Checkout is Phase 7, so the call to action is disabled for now. */
export default async function CreatorPage({ params }: Props) {
  const { slug } = await params;
  const creator = await loadCreator(slug);
  const { pricing, goal } = await loadExtras(creator.slug);
  return (
    <>
      <script
        type="application/ld+json"
        // A data block, not a script: CSP does not apply. `<` is escaped so a name cannot close the element.
        dangerouslySetInnerHTML={{
          __html: JSON.stringify(jsonLd(creator.name, creator.slug)).replace(/</g, "\\u003c"),
        }}
      />
      <CreatorView creator={creator} pricing={pricing} goal={goal} />
    </>
  );
}
