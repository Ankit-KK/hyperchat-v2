import type { ReactNode } from "react";
import { serverApi } from "../../lib/api";

/** The public host's frame. The contact block comes from GET /api/v1/public/config; anything missing is hidden. */
export default async function PublicLayout({ children }: { children: ReactNode }) {
  const config = await serverApi().get("/api/v1/public/config");
  const contact = config.ok ? config.data.contact : null;
  return (
    <>
      <header className="border-b border-border">
        <div className="mx-auto max-w-5xl px-4 py-4 text-lg font-semibold">HyperChat</div>
      </header>
      {children}
      <footer className="mt-16 border-t border-border">
        <div className="mx-auto max-w-5xl px-4 py-8 text-sm text-muted-foreground">
          {contact && (contact.name || contact.email || contact.phones.length > 0 || contact.discord_url) ? (
            <address className="not-italic" aria-label="Contact">
              <h2 className="mb-2 font-medium text-foreground">Contact</h2>
              {contact.name && <p>{contact.name}</p>}
              {contact.email && (
                <p>
                  <a className="underline" href={`mailto:${contact.email}`}>
                    {contact.email}
                  </a>
                </p>
              )}
              {contact.phones.map((phone) => (
                <p key={phone}>
                  <a className="underline" href={`tel:${phone.replace(/\s+/g, "")}`}>
                    {phone}
                  </a>
                </p>
              ))}
              {contact.discord_url && (
                <p>
                  <a className="underline" href={contact.discord_url} rel="noopener noreferrer">
                    Discord
                  </a>
                </p>
              )}
            </address>
          ) : null}
        </div>
      </footer>
    </>
  );
}
