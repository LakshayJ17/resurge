import Link from "next/link";

export function SiteFooter() {
  return (
    <footer className="site-foot">
      <div className="wrap">
        <p>
          Resurge writes its reports with AI from public web pages. Every fact is checked against its source, but
          mistakes are possible: confirm important figures before you rely on them. Not affiliated with the companies
          it researches.
        </p>
        <nav aria-label="Footer">
          <Link href="/#how">How it works</Link>
          <Link href="/privacy">Privacy</Link>
        </nav>
      </div>
    </footer>
  );
}
