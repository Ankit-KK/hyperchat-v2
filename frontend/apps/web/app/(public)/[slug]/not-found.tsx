/** An unknown or inactive creator (the API answered 404). Next.js sends 404 with noindex. The website's tone. */
export default function CreatorNotFound() {
  return (
    <main className="mx-auto max-w-xl px-4 py-16">
      <p className="text-sm tracking-widest text-muted-foreground uppercase">Off air</p>
      <h1 className="mt-2 text-3xl">Stream not found</h1>
      <p className="mt-3 text-muted-foreground">
        This channel is off air. The creator may have moved, or the link may be mistyped.
      </p>
      {/* Plain links: both pages are the marketing website, so client-side navigation cannot load them. */}
      {/* eslint-disable @next/next/no-html-link-for-pages */}
      <nav aria-label="Elsewhere on HyperChat" className="mt-6 flex gap-6">
        <a href="/" className="underline underline-offset-4">
          Back to home
        </a>
        <a href="/support" className="underline underline-offset-4">
          Need a hand?
        </a>
      </nav>
      {/* eslint-enable @next/next/no-html-link-for-pages */}
    </main>
  );
}
