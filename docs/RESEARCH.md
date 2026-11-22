# Research notes behind Resurge (November 2026)

These notes record what was researched before building, what was chosen, and why. Free tiers change month to month,
so treat the numbers as a dated snapshot. `resurge doctor` checks your actual limits.

## 1. Free LLM providers

| Provider | What we found | Decision |
|---|---|---|
| **Groq** | No-card free tier. Production models: `openai/gpt-oss-120b`, `openai/gpt-oss-20b`. Preview: `qwen/qwen3.8-27b`. (Update 2026-11-21: `llama-3.1-8b-instant` and `llama-3.3-70b-versatile` were deprecated on 2026-06-17 and shut down for on-demand use on 2026-08-16; they and `minimaxai/minimax-m2.7` / `meta-models/muse-spark-open` are still on Groq's model page but marked "Contact sales" and are not served to free keys.) Published base limits for gpt-oss-120b: 30 RPM, 1K RPD, **8K TPM**, 200K TPD. `x-ratelimit-*-requests` = RPD, `*-tokens` = TPM; cached tokens don't count. | Primary for short, fast calls (planning, verification, QA, reflection). The router caps `max_tokens` so prompt + completion fits TPM. |
| **Google AI Studio** | Current IDs: `gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, `gemini-3-flash-preview`, `gemini-3.1-pro-preview`; Gemini 4 (Carbon/Argon/Hydrogen) is not free. 2.5 Flash/Pro are deprecated (routing to 3.x on 2026-12-01). Gemma 4 (`gemma-4-31b-it`, `gemma-4-26b-a4b-it`) is free. Reported free limits: 3.x Flash ≈ 5 RPM / 20 RPD, Flash-Lite ≈ 15 RPM / 500 RPD, Gemma 4 ≈ 15 RPM / 1.5K RPD. Google Search grounding is free only on deprecated 2.5 Flash/Flash-Lite. Free-tier data may be used for training (outside EU/UK/EEA). Quotas reset at midnight Pacific. | Long-context extraction (Flash-Lite, Gemma 4); 3.8 Flash reserved for the CEO's summary. Limiter uses the Pacific day boundary. Marked `trains_on_data`. |
| **OpenRouter** | The public catalog (`/api/v1/models`, keyless) listed **17 free models** on 2026-11-15, e.g. `nvidia/nemotron-3-ultra-550b-a55b:free`, `nvidia/nemotron-3.5-super-vl:free`, `google/gemma-4-31b-it:free`, `baidu/ernie-5.5-flash-preview:free`, `inclusionai/ling-3.2-flash:free`, plus the `openrouter/free` auto-router. Limits: 20 RPM; **50 requests/day shared** across all free models (1,000/day after $10 lifetime credits). | Rare, high-value reasoning (QA, risk). Provider-level RPD of 50. The `:free` catalog is auto-discovered at startup and vanished models are dropped. |
| **Mistral** | Free plan with restrictive per-model limits (shown in the console); free-plan data may be used for training. | Writing / extraction fallback; marked `trains_on_data`. |
| **NVIDIA NIM** | Free developer access at `integrate.api.nvidia.com/v1`, ~40 RPM (account-wide, not guaranteed), namespaced model IDs. | Extra capacity for gpt-oss-120b / Llama-3.3-70B. |
| **Cloudflare Workers AI** | 10K neurons/day free. | Small overflow. |
| **Cerebras** | No permanent free tier any more: trial credits (~$5 / 30 days). | Optional; marked `free: trial`. |
| **GitHub Models** | Reportedly retired 2026-07-30. | Not included. |
| **Ollama (local)** | Unlimited and private; quality depends on the model (e.g. qwen2.5:7b on a 16 GB laptop). | Last-resort fallback for every role; `--local` mode. Native `/api/chat` with fixed `num_ctx` (the OpenAI-compatible endpoint can silently truncate context). |

All of these except Ollama's native API speak the OpenAI `/chat/completions` dialect. So Resurge uses one lean `httpx`
client instead of a heavy multi-provider SDK, and handles each provider's quirks with learned per-model capability
flags.

## 2. Free search & data sources

| Source | Free tier | Use |
|---|---|---|
| DuckDuckGo via `ddgs` 9.x | Free, no key, but scrapes search-engine endpoints unofficially | **Opt-in only** (`RESURGE_ALLOW_UNOFFICIAL_SEARCH=1`): may conflict with those engines' terms and gets IPs blocked |
| **Parallel hosted MCP** (`https://mcp.parallel.ai/mcp`) | No account. Verified tools: `web_search {objective, search_queries[]}`, `web_fetch {urls[≤20], objective?, full_content?}`, plus `web_decisions`, `find_verticals`, `search_vertical`, `image_search`; results carry url, title, publish_date and excerpts | Second web backend + fetch fallback (it also reaches G2, Capterra and Crunchbase pages that block plain HTTP clients); also the built-in MCP-client integration |
| Google News RSS | No key, but its robots.txt disallows `/rss/` for generic bots and the feed is licensed for personal, non-commercial readers | **Off** (opt-in flag, and it still obeys robots.txt). News comes from Parallel and keyed APIs |
| Hacker News (Algolia) | Free, no key | Community sentiment, tech buzz |
| Wikipedia | Free, no key | Company overviews |
| GitHub REST | 60 req/h keyless, 5K with token | Open-source footprint |
| Jina Reader (`r.jina.ai`) | Keyless at low rate; 10M tokens per free key | Fetch fallback for JS-heavy pages |
| Tavily | 1,000 credits/month | Premium web search if a key is present |
| Exa | $10 credit/month | Premium web search |
| Serper | 2,500 one-time queries | Google results |
| Brave | Free plan replaced by $5 monthly credit (Feb 2026), card required | Optional |
| SearXNG | Self-hosted, unlimited | Optional |
| Google CSE | Closed to new customers | Not used |

Results from several backends are merged with Reciprocal Rank Fusion and deduplicated by normalised URL.

Verified live on 2026-11-15: ddgs text and news, Google News RSS, Hacker News, Wikipedia, GitHub, Parallel MCP search and
fetch, keyless Jina Reader, and direct fetch with trafilatura extraction. Key-based backends (Tavily, Exa, Serper, Brave,
Jina search, SearXNG) are implemented from their documented payloads and unit-tested against sample responses.
`robots.txt` is always honoured, and sites whose terms forbid automated collection (Reddit, LinkedIn, G2, Crunchbase, paywalled news, …) are snippet-only. See [COMPLIANCE.md](COMPLIANCE.md).
Jina Reader is asked for rendered HTML and extracted locally with trafilatura, because Jina's own markdown carried about
3× more navigation boilerplate.

## 3. Multi-agent design choices

* **Lean custom orchestration instead of CrewAI/AutoGen ReAct loops.** Free tiers are capped on requests and tokens per
  minute. Free-form ReAct agents spend many calls on tool chatter. A department pipeline with bounded agent loops
  (search → select → read → extract → verify → gap-close) gets the same depth for a fraction of the calls.
  `asyncio` concurrency is bounded per provider.
* **Deterministic agents.** CFO, CTO router, Validation Engine, visualisation, publishing and the Playbook Curator are
  code. Never spend tokens on what code can do reliably.
* **Different model families per department + cross-family verification.** This reduces correlated errors, and it
  spreads load across many free quotas.

## 4. Validation research

* **Evidence-first extraction with verbatim quotes.** It enables a zero-token grounding check that catches fabricated
  numbers before any LLM verification.
* **LLM-as-judge with rubrics, using a different judge model** than the producer, plus revision loops.
* **Confidence = verdict × source reliability + corroboration − staleness.** Source reliability is learned (Bayesian
  update from verdicts).

## 5. Self-improvement research

* **ACE — Agentic Context Engineering** (ICLR 2026, arXiv:2510.04618) treats context as an evolving *playbook*:
  Generator → Reflector → Curator, itemised entries with helpful/harmful counters, incremental delta updates, and
  dedupe/prune to avoid context collapse. It reports +10.6% on agent benchmarks and beats GEPA on finance tasks. Its
  documented weakness: without reliable feedback it can **degrade**. Resurge feeds it validated signals only (grounding
  failures, verifier verdicts, QA rubric scores, coverage), caps playbooks at 12 lessons per role and retires harmful
  lessons.
* **Bandit routing.** Thompson sampling over per-(role, model) success and validated-quality statistics. Strong models
  win traffic and newly discovered free models still get explored.

## 6. Product refinements adopted from CI-tool research

Commercial CI platforms (Klue, Crayon, Kompyte) centre on **battlecards**, **change monitoring** and **pricing-page
tracking**. Intelligence goes stale fast, so Resurge adds:
* battlecards with objection handling for sales enablement;
* `resurge watch`, which re-runs and reports *new facts since the last engagement*, with a free GitHub Actions
  schedule and webhook alerts;
* a knowledge base for incremental, cheaper re-runs and for MCP follow-up questions;
* a risk office that also audits the research's own blind spots.

## Sources

- Groq rate limits — https://console.groq.com/docs/rate-limits · models — https://console.groq.com/docs/models
- OpenRouter model catalog (live) — https://openrouter.ai/api/v1/models · free API comparison — https://openrouter.ai/blog/tutorials/free-llm-apis-compared/
- OpenRouter free limits — https://www.developersdigest.tech/rate-limits/openrouter
- Gemini models — https://aistudio.google.com/docs/models · rate limits — https://ai.google.dev/gemini-api/docs/rate-limits
- Gemini free tier analysis — https://www.memetik.ai/guides/gemini-api-free-tier-limits
- Gemma 4 on the Gemini API — https://free-llm.com/models/google-ai-studio/gemma-4-31b-it
- Cerebras rate limits — https://inference-docs.cerebras.ai/support/rate-limits
- Mistral tiers — https://docs.mistral.ai/deployment/ai-studio/tier
- GitHub Models status — https://fast.io/resources/github-models-rate-limit/
- NVIDIA NIM limits — https://news.creeta.com/en/nvidia-nim-free-tier-limits-2026/
- Free search APIs — https://parallel.ai/articles/best-free-web-search-api · https://www.firecrawl.dev/blog/best-free-web-search-apis
- ACE — https://arxiv.org/abs/2510.04618
- AI CI agent blueprint — https://resources.rework.com/libraries/ai-agents/ai-competitive-intelligence-agent
