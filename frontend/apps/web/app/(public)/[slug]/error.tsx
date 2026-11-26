"use client";

/**
 * The API could not be reached (CreatorUnavailable) or rendering failed. Next.js answers these with 500, not 503:
 * an App Router page cannot choose its status code. Never indexed: the server HTML is Next.js's noindex error
 * shell, and this boundary renders in the browser (the meta tag below covers client-side navigation).
 */
export default function CreatorUnavailablePage({ reset }: { error: Error; reset: () => void }) {
  return (
    <main className="mx-auto max-w-xl px-4 py-16">
      <meta name="robots" content="noindex" />
      <p className="text-sm tracking-widest text-muted-foreground uppercase">Signal lost</p>
      <h1 className="mt-2 text-3xl">This page is temporarily unavailable</h1>
      <p className="mt-3 text-muted-foreground">
        We could not reach HyperChat just now. Please try again in a moment.
      </p>
      <button type="button" className="mt-6 underline underline-offset-4" onClick={reset}>
        Try again
      </button>
    </main>
  );
}
