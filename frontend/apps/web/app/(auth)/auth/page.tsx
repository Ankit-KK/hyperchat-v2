"use client";

/** auth. host shell (served at "/" on that host). Sign-in, invites, registration and reset arrive in Phase 3. */
export default function AuthShell() {
  return (
    <main className="mx-auto max-w-md px-4 py-16">
      <h1 className="text-2xl font-semibold">Sign in to HyperChat</h1>
      <p className="mt-2 text-muted-foreground">Sign-in is not available yet.</p>
    </main>
  );
}
