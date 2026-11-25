"use client";

import { Alert } from "@hyperchat/ui";
import { useEffect, useState } from "react";
import { browserApi } from "../../../lib/browser-api";

type State =
  | { kind: "loading" }
  | { kind: "signedIn"; user: Record<string, unknown> }
  | { kind: "signedOut" }
  | { kind: "error"; message: string };

/**
 * dashboard. host shell with the session bootstrap stub: GET /api/v1/auth/session with the API's cookie. A signed-out
 * visitor is only told so; sending them to auth. is Phase 3. Frontend checks are user experience, never authorization.
 */
export default function DashboardShell() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let live = true;
    browserApi.session().then((result) => {
      if (!live) return;
      if (result.ok) setState({ kind: "signedIn", user: result.data.user });
      else if (result.error.kind === "unauthorized") setState({ kind: "signedOut" });
      else setState({ kind: "error", message: result.error.error });
    });
    return () => {
      live = false;
    };
  }, []);

  return (
    <main className="mx-auto max-w-5xl px-4 py-12">
      <h1 className="text-2xl font-semibold">Dashboard</h1>
      <div className="mt-6" aria-live="polite">
        {state.kind === "loading" && <p className="text-muted-foreground">Checking your session…</p>}
        {state.kind === "signedIn" && (
          <Alert>Signed in{typeof state.user.email === "string" ? ` as ${state.user.email}` : ""}.</Alert>
        )}
        {state.kind === "signedOut" && <Alert>You are not signed in.</Alert>}
        {state.kind === "error" && <Alert tone="error">{state.message}</Alert>}
      </div>
    </main>
  );
}
