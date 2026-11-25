"use client";

export default function ErrorPage({ reset }: { error: Error; reset: () => void }) {
  return (
    <main className="mx-auto max-w-xl p-8">
      <h1 className="text-2xl font-semibold">Something went wrong</h1>
      <p className="mt-2 text-muted-foreground">This page could not be loaded. Please try again.</p>
      <button type="button" className="mt-4 underline" onClick={reset}>
        Try again
      </button>
    </main>
  );
}
