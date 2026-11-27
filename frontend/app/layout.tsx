import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import { DemoBanner, Providers } from "./providers";
import "./globals.css";

export const metadata: Metadata = {
  title: "Pixel & Volt | Relay support demo",
  description: "A customer-support agent demo for a fictional electronics store.",
};

export const viewport: Viewport = { width: "device-width", initialScale: 1 };

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-slate-50 text-slate-900 antialiased">
        <Providers>
          <a
            href="#main"
            className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-white focus:px-3 focus:py-2"
          >
            Skip to content
          </a>
          <DemoBanner />
          {children}
        </Providers>
      </body>
    </html>
  );
}
