import { ImageResponse } from "next/og";
import { accentColor, imageSrc, loadCreator } from "./creator";

export const alt = "Support this creator on HyperChat";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

/** 1200x630 card from the creator's name, colour and profile picture, in the website palette. */
export default async function Image({ params }: { params: Promise<{ slug: string }> }) {
  const creator = await loadCreator((await params).slug);
  const color = accentColor(creator.brand_color);
  const accent = color.startsWith("#") ? color : "#b8702f"; // next/og has no CSS variables
  // next/og fetches the picture itself, with its own SSRF guard (no private or local addresses).
  const src = imageSrc(creator.brand_logo_url);
  return new ImageResponse(
    <div
      style={{
        width: "100%",
        height: "100%",
        display: "flex",
        alignItems: "center",
        gap: 56,
        padding: 80,
        background: "#221f1c",
        color: "#f3ede2",
        borderBottom: `16px solid ${accent}`,
      }}
    >
      <div
        style={{
          width: 280,
          height: 280,
          borderRadius: 140,
          border: `10px solid ${accent}`,
          overflow: "hidden",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background: "#2e2a26",
          fontSize: 140,
          flexShrink: 0,
        }}
      >
        {src ? (
          // eslint-disable-next-line @next/next/no-img-element -- next/og renders this to a PNG
          <img src={src} alt="" width={260} height={260} style={{ objectFit: "cover" }} />
        ) : (
          creator.name.charAt(0)
        )}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 20, minWidth: 0 }}>
        <div style={{ fontSize: creator.name.length > 18 ? 64 : 88, lineHeight: 1.05 }}>{creator.name}</div>
        <div style={{ fontSize: 36, color: "#b6ac9f" }}>Support on HyperChat</div>
      </div>
    </div>,
    size,
  );
}
