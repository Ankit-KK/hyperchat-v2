import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";

// Rendered per request so every script carries the proxy's CSP nonce.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "HyperChat administration",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
