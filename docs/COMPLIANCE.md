# Security & compliance policy

Resurge collects **public** information about **companies**, the way an analyst with a browser would, only slower
and more politely. This document explains what it does and doesn't do. It is not legal advice: laws and site terms
differ by country and change. If you research in a regulated context, check with your counsel.

## 1. Lawful, polite collection: "don't get blocked" by being a good citizen

Resurge avoids blocks by **not behaving badly**. It never evades blocks.

| Practice | What Resurge does |
|---|---|
| Identity | Honest bot User-Agent (`ResurgeBot/0.1`), plus your contact if you set `RESURGE_CONTACT`. No disguises. |
| robots.txt | **Always honoured** (RFC 9309), with no switch to turn it off. `Crawl-delay` is respected. If robots.txt can't be read because of a server error, the site is treated as off-limits for now. |
| Pace | One request at a time per site, at least 2s apart (longer if the site asks). Bounded concurrency overall. |
| Back-off | `429`/`503` honour `Retry-After` and retry at most once. Sites that keep refusing are skipped for the rest of the run (circuit breaker). |
| Refusals | `401`/`403`/`451` and robots.txt disallows are final. Resurge **never** routes around a refusal via another fetcher. |
| Never | No proxy or IP rotation, no CAPTCHA solving, no fingerprint spoofing, no logins, no paywall bypass, no private or personal data. |
| Site terms | Sites whose terms forbid automated collection are never fetched, directly or via another service. That covers social and professional networks, job and review sites (Glassdoor, G2, Capterra, Trustpilot, Yelp), company-data vendors (Crunchbase, PitchBook, ZoomInfo), Reddit, Quora, Medium, YouTube, Amazon and hard-paywalled news (NYT, WSJ, FT, Bloomberg and others). At most their search snippets are used. The list lives in `config/compliance.yaml`: you can add domains, or record exceptions you hold written permission for. `RESURGE_EXTRA_NOFETCH_DOMAINS` extends it per run. |
| Search | Only sanctioned access paths by default: official APIs (Tavily, Exa, Brave, Serper, Jina, your SearXNG), Parallel's free MCP, and the official Hacker News, Wikipedia and GitHub APIs. Unofficial search-engine scraping (`ddgs`) and Google News RSS are **off** unless you opt in with `RESURGE_ALLOW_UNOFFICIAL_SEARCH=1`. ddgs can conflict with search engines' terms and gets IPs blocked. Google News RSS's robots.txt disallows bots and its licence covers personal feed readers, so even with the opt-in it stays robots-blocked. |
| Volume | Caching (search 24h, pages 3 days), a knowledge base and per-run budgets keep request counts low. |

Rendering services (Jina Reader, Parallel `web_fetch`) are used only to read pages the site served normally but
that rendered empty with plain HTTP (JavaScript shells), PDFs served with HTTP 200, and pages after network errors or
timeouts. They are never used after a refusal (robots.txt, 401/403/429/451, terms list).

**Set `RESURGE_CONTACT`** (URL or e-mail) so site owners can reach you. Wikimedia's API policy asks for it. Each run's
methodology reports how many pages were skipped for robots.txt, site terms, access denial or rate limiting.

## 2. Data protection

* **Companies, not people.** The research dimensions concern products, prices, funding, positioning and sentiment.
  Named executives appear only as public business facts.
* **PII redaction.** E-mail addresses and phone numbers are redacted from every page *before* it is stored,
  sent to an LLM or written to a report.
* **Your brief.** Some free LLM tiers (Gemini free tier, Mistral free plan) may use prompts for training. Use
  `--private` to exclude them, or `--local` to keep everything on your machine. Don't put confidential strategy into
  briefs sent to such providers.
* **Retention.** Pages and search results expire from the cache automatically. `resurge purge` deletes caches, and
  with flags also the knowledge base, learning state, history or reports.

## 3. Copyright & attribution

Reports contain short verbatim quotes (5 to 40 words) used as evidence for analysis, always with a link to the source,
plus Resurge's own analysis. Full pages are never republished; the local page cache is private and expires.

## 4. LLM provider terms

Each provider is used with **one** account and kept **inside** its published free-tier limits. The router reads
rate-limit headers and backs off. Resurge never pools multiple accounts to multiply free quotas. NVIDIA's free
endpoints are intended for development and testing, so treat them accordingly.

## 5. Prompt-injection resistance

Web pages are attacker-controlled. Defences, cheapest first:

1. **Least privilege.** LLM agents have no tools: they can't browse, fetch, write files, send messages or call
   APIs. Code decides every action, so injected text can at most try to distort extracted data.
2. **Sanitisation.** Invisible and control characters (zero-width, bidi overrides, the Unicode "tag" block used for
   ASCII smuggling), chat-template and role tokens, and instruction-like sentences ("ignore previous instructions...",
   "you are now...", "mark every claim as supported...", "reveal your system prompt...") are removed before any model sees
   the text.
3. **Scoring & quarantine.** Every chunk gets a weighted injection score. High-risk chunks never reach a model.
   Borderline chunks are neutralised, labelled "CAUTION" to the model, and their facts lose confidence.
4. **Spotlighting.** Evidence is wrapped in blocks fenced with a random per-request nonce
   (`<<EVIDENCE E-1 #a1b2c3d4>> ... <<END E-1 #a1b2c3d4>>`). A page can't guess the nonce, so it can't close its block
   and speak as the system. Fence-like sequences inside content are defanged. Every agent's standing rules say that
   block content is data, never instructions.
5. **Output validation.** Extracted claims or quotes containing instruction-like text are rejected. Every fact must
   still pass deterministic quote grounding and fact-checking by a different model family.
6. **Optional ML screen.** With a Groq key, the Security & Compliance Officer asks Llama Prompt Guard 2 (a free
   prompt-injection classifier) for a second opinion on borderline chunks. That role never falls back to a general
   chat model.
7. **Safe deliverables.** Google Sheets cells are written as raw values, so web text is never run as a formula. The HTML report
   ships a strict Content-Security-Policy, escapes all text and allows only `http(s)` links. Markdown escapes raw
   HTML. Chat notifications suppress `@everyone` / `@here`.

Every run reports the counts (quarantined, neutralised, rejected, redacted, restricted pages skipped) in its
methodology section. Any injection attempt becomes an entry in the risk register.
