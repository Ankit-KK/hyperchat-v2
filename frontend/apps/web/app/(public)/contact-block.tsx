import type { Schemas } from "@hyperchat/api-client";

/** The contact block from GET /api/v1/public/config (D5). Every field may be empty: show only what is set. */
export function ContactBlock({ contact }: { contact: Schemas["PublicContact"] | null }) {
  if (!contact || !(contact.name || contact.email || contact.phones.length > 0 || contact.discord_url))
    return null;
  return (
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
  );
}
