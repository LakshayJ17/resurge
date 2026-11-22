import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = { title: "Page not found" };

export default function NotFound() {
  return (
    <div className="wrap page">
      <header className="page-head">
        <h1>Page not found</h1>
        <p className="lede">The link may be old, or the page belongs to another account.</p>
      </header>
      <p><Link href="/">Back to Resurge</Link></p>
    </div>
  );
}
