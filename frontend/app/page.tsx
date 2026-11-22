import { Flowchart } from "@/components/Flowchart";
import { HeroActions } from "@/components/HeroActions";

const GET = [
  { title: "An action plan", text: "Each move comes with a first step, an owner and a way to measure it." },
  { title: "Pricing side by side", text: "What a team of 10, 50 or 200 people would pay a year with each competitor." },
  { title: "A page per competitor", text: "Why buyers pick them, why they pick you, and how to win the deal." },
  { title: "Answers with sources", text: "Ask anything about the research; every answer cites the facts it came from." },
];

const USE = [
  { title: "Create a free account", text: "Sign up with your email and a password. Nothing to install, no card." },
  { title: "Type a company or its website",
    text: "For example Notion or linear.app. Optionally name the competitors you care about, add up to three "
      + "questions and pick how deep to go." },
  { title: "Watch it work", text: "The page shows each step as it happens. You can close it: the research keeps running." },
  { title: "Read and ask",
    text: "Open the report, download it as a PDF or slide deck, and ask follow-up questions." },
];

const PDF = [
  { title: "Starts with the company", text: "Who it is and what it sells, before any competitor." },
  { title: "Every chapter", text: "Summary, action plan, market, pricing, features, a chapter per competitor and the risks." },
  { title: "Easy to check", text: "Clickable contents and bookmarks; each citation jumps to the exact quote and its source." },
];

const UNDER_THE_HOOD = [
  { title: "Polite, lawful crawling",
    text: "Follows robots.txt and each site's terms, waits between requests, and never logs in or gets around a block." },
  { title: "Guarded against prompt injection",
    text: "Your input and every web page are screened before any AI reads them; answers that leak instructions are replaced." },
  { title: "Free AI models with fallback",
    text: "Runs on the free tiers of Groq, Google Gemini and OpenRouter. When one model is busy, the next takes over." },
];

export default function Landing() {
  return (
    <div className="wrap">
      <section className="hero">
        <div>
          <h1>Competitor research you can check</h1>
          <p className="lede">
            Type a company or its website. Resurge&apos;s AI agents find its competitors, read their public pages,
            check every fact against its source and give you a report you can question.
          </p>
          <HeroActions />
          <p className="meta">Free to use · a quick analysis takes about 10–20 minutes</p>
        </div>
        <figure className="preview">
          <img src="/report-preview.png" width={1280} height={1000}
            alt="A Resurge report: the action plan page with competitors, pricing and risks in the sidebar" />
          <figcaption>Every report opens on an action plan, with competitors, pricing and risks a click away.</figcaption>
        </figure>
      </section>

      <section aria-labelledby="what">
        <h2 id="what">What you get</h2>
        <div className="grid-4">
          {GET.map((g) => (
            <div className="item" key={g.title}><h3>{g.title}</h3><p>{g.text}</p></div>
          ))}
        </div>
        <p className="note" style={{ marginTop: 20 }}>
          Also as a PDF to share and a slide deck that opens in PowerPoint, Keynote and Google Slides.
        </p>
      </section>

      <section aria-labelledby="pdf" className="pdf-feature">
        <div>
          <h2 id="pdf">Get the full report as a PDF</h2>
          <p className="lede">
            One document to read offline, print or send to your team, downloaded from the report page in one click.
          </p>
          <ul className="ticks">
            {PDF.map((p) => <li key={p.title}><strong>{p.title}.</strong> {p.text}</li>)}
          </ul>
        </div>
        <div className="pdf-pages">
          <img className="pg pg-3" src="/pdf-evidence.webp" width={600} height={849} loading="lazy"
            alt="Evidence appendix: each fact with its exact quote, source and confidence" />
          <img className="pg pg-2" src="/pdf-summary.webp" width={600} height={849} loading="lazy"
            alt="Executive summary page with key findings, threats and opportunities" />
          <img className="pg pg-1" src="/pdf-cover.webp" width={600} height={849} loading="lazy"
            alt="PDF cover with the headline and the number of competitors, facts and sources" />
        </div>
      </section>

      <section aria-labelledby="use">
        <h2 id="use">How to use it</h2>
        <ol className="steps">
          {USE.map((u) => (
            <li key={u.title}><div><h3>{u.title}</h3><p>{u.text}</p></div></li>
          ))}
        </ol>
      </section>

      <section aria-labelledby="how-title" id="how">
        <h2 id="how-title">How it works</h2>
        <p className="lede measure" style={{ marginBottom: 28 }}>
          Twenty-two AI agents, organised like a small research firm, run each analysis in stages. A fact reaches your
          report only if it was found on a public page and passed two checks.
        </p>
        <Flowchart />
        <div className="grid-3">
          {UNDER_THE_HOOD.map((u) => (
            <div className="item" key={u.title}><h3>{u.title}</h3><p>{u.text}</p></div>
          ))}
        </div>
      </section>

      <section className="cta">
        <h2>Try it on a company you know</h2>
        <HeroActions secondary={false} />
      </section>
    </div>
  );
}
