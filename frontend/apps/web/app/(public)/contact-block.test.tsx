import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ContactBlock } from "./contact-block";

describe("ContactBlock", () => {
  it("renders every field that is set", () => {
    const html = renderToStaticMarkup(
      <ContactBlock
        contact={{
          name: "Streamheart",
          email: "support@example.invalid",
          phones: ["+91 90000 00000"],
          discord_url: "https://discord.gg/example",
        }}
      />,
    );
    expect(html).toContain('aria-label="Contact"');
    expect(html).toContain("Streamheart");
    expect(html).toContain('href="mailto:support@example.invalid"');
    expect(html).toContain('href="tel:+919000000000"');
    expect(html).toContain('href="https://discord.gg/example"');
  });

  it("hides missing fields, and the whole block when nothing is set or the API failed", () => {
    expect(
      renderToStaticMarkup(
        <ContactBlock contact={{ name: null, email: "a@example.invalid", phones: [], discord_url: null }} />,
      ),
    ).not.toMatch(/Discord|tel:/);
    expect(
      renderToStaticMarkup(
        <ContactBlock contact={{ name: null, email: null, phones: [], discord_url: null }} />,
      ),
    ).toBe("");
    expect(renderToStaticMarkup(<ContactBlock contact={null} />)).toBe("");
  });
});
