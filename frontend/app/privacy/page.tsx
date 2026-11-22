import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = { title: "Privacy" };

export default function Privacy() {
  return (
    <div className="wrap page">
      <header className="page-head">
        <h1>Privacy</h1>
        <p className="lede">What Resurge keeps, who else sees it, and how to remove it.</p>
      </header>

      <section aria-labelledby="keep">
        <h2 id="keep">What is stored</h2>
        <ul className="measure">
          <li>Your email address and a salted hash of your password (never the password itself).</li>
          <li>The analyses you start and their reports, so you can open them again.</li>
          <li>One session cookie that keeps you logged in. There are no advertising or tracking cookies and no analytics.</li>
          <li>A security log of log-ins, blocked requests and IP addresses, used only for limits and to stop abuse.</li>
        </ul>
      </section>

      <section aria-labelledby="share">
        <h2 id="share">Who else sees what you type</h2>
        <ul className="measure">
          <li>
            Company names, questions and chat messages are sent to free AI services (Groq, Google Gemini,
            OpenRouter) to do the research. On free tiers these services may keep and review what they receive,
            so don&apos;t enter confidential information.
          </li>
          <li>Search terms are sent to web search services to find public pages.</li>
          <li>
            When you choose a password, only the first five characters of its SHA-1 hash are sent to Have I Been
            Pwned to check it against known breaches.
          </li>
        </ul>
      </section>

      <section aria-labelledby="remove">
        <h2 id="remove">Removing your data</h2>
        <p className="measure">
          <Link href="/account">Delete your account</Link> at any time: your account, your analyses and their
          reports are removed. Security log entries are kept for a short time to stop abuse.
        </p>
      </section>

      <section aria-labelledby="reports">
        <h2 id="reports">About the reports</h2>
        <p className="measure">
          Reports are written by AI from public web pages. Every fact is checked against its source and by a second
          model, but mistakes are possible: confirm important figures before you rely on them. Resurge is not
          affiliated with the companies it researches.
        </p>
      </section>
    </div>
  );
}
