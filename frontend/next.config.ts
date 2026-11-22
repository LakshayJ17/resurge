import type { NextConfig } from "next";
import { PHASE_DEVELOPMENT_SERVER } from "next/constants";

// Production: a static export (frontend/out) that `resurge serve` hosts on the same origin as its API, so the session
// cookie, Origin checks and Content-Security-Policy cover every page.
// Development (`npm run dev`): the API calls are proxied to a running `resurge serve` (RESURGE_API_URL); start that
// server with RESURGE_ALLOWED_ORIGINS=http://localhost:3000 so it accepts form posts from the dev server.
export default function config(phase: string): NextConfig {
  if (phase === PHASE_DEVELOPMENT_SERVER) {
    const api = process.env.RESURGE_API_URL ?? "http://127.0.0.1:8000";
    return {
      reactStrictMode: true,
      async rewrites() {
        return [
          { source: "/api/:path*", destination: `${api}/api/:path*` },
          { source: "/a/:path*", destination: `${api}/a/:path*` },
        ];
      },
    };
  }
  return {
    output: "export",
    reactStrictMode: true,
    trailingSlash: false,
    images: { unoptimized: true },
  };
}
