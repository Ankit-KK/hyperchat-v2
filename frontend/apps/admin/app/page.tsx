/** Placeholder signed-out page. The admin sign-in, passkey step-up and every admin route arrive in Phase 8. */
export default function SignedOut() {
  return (
    <main className="mx-auto max-w-md px-4 py-16">
      <h1 className="text-2xl font-semibold">HyperChat administration</h1>
      <p className="mt-2 text-muted-foreground">
        You are signed out. Administrator sign-in is not available yet.
      </p>
    </main>
  );
}
