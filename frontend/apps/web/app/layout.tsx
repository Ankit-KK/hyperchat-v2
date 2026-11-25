import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";

// Every page renders per request: the proxy's CSP nonce only reaches scripts rendered at request time.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: { default: "HyperChat", template: "%s | HyperChat" },
  description: "Interactive donations and livestream alerts for creators.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
