import type { ReactNode } from "react";
import { serverApi } from "../../lib/api";
import { ContactBlock } from "./contact-block";

/** The public host's frame. The contact block comes from GET /api/v1/public/config; anything missing is hidden. */
export default async function PublicLayout({ children }: { children: ReactNode }) {
  const config = await serverApi().get("/api/v1/public/config");
  return (
    <>
      <header className="border-b border-border">
        <div className="mx-auto max-w-5xl px-4 py-4 text-lg font-semibold">HyperChat</div>
      </header>
      {children}
      <footer className="mt-16 border-t border-border">
        <div className="mx-auto max-w-5xl px-4 py-8 text-sm text-muted-foreground">
          <ContactBlock contact={config.ok ? config.data.contact : null} />
        </div>
      </footer>
    </>
  );
}
