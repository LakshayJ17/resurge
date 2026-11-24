import type { Metadata, Viewport } from "next";
import { SessionProvider } from "@/components/SessionProvider";
import { SiteFooter } from "@/components/SiteFooter";
import { SiteHeader } from "@/components/SiteHeader";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Resurge: competitor research you can check", template: "%s | Resurge" },
  description: "Type a company or its website. AI agents research its competitors from public sources, check every "
    + "fact against its source and write a report you can question.",
  referrer: "same-origin",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#fbfaf7" },
    { media: "(prefers-color-scheme: dark)", color: "#151514" },
  ],
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <SessionProvider>
          <SiteHeader />
          <main id="top">{children}</main>
          <SiteFooter />
        </SessionProvider>
      </body>
    </html>
  );
}
