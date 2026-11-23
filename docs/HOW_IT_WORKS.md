# How Resurge works

This is the technical companion to the [README](../README.md). It covers the architecture, every stage of a run,
how models are chosen, the agent roster, how claims are verified, the guardrails, and how each run improves the next.

**Contents:** [Architecture](#architecture) · [A run, start to finish](#a-run-start-to-finish) ·
[One research round](#inside-one-research-round) · [Model routing](#how-a-model-is-picked-for-each-call) ·
[The agents](#the-agents) · [Validation](#validation) · [Guardrails](#guardrails) ·
[Self-improvement](#self-improvement) · [Staying free](#staying-free) · [Performance](#cost-and-performance) ·
[Hosted app flow](#hosted-app-request-flow) · [Example run](#example-run)

---

## Architecture

Four ways in, one engine. Agents never call a model or the web directly: every model call goes through the gateway
(routing, quotas, failover) and every page goes through the fetcher (robots.txt, site terms, safe addresses).

```mermaid
flowchart TB
  subgraph IN["Ways in"]
    direction LR
    CLI["CLI<br/>resurge run · chat"] ~~~ DASH["Local dashboard<br/>resurge ui"] ~~~ WEB["Hosted web app<br/>resurge serve"] ~~~ MCPS["MCP server<br/>resurge mcp"]
  end

  IN --> CHECK["Input checks<br/>rules · Prompt Guard 2 · policy model"]
  CHECK --> COO["COO orchestrator<br/>12 stages · shared run context · CFO budget"]
  CHECK --> CHAT["Chat (RAG)<br/>hybrid retrieval · cited answers"]
  COO --> AGENTS["22 agents in 10 departments<br/>Validation Engine checks every hand-off"]

  AGENTS --> TOOLS["Research tools<br/>search router · polite fetcher · Security Officer"]
  AGENTS --> GW["LLM gateway (CTO)<br/>router · quotas · failover · JSON repair · cache"]
  AGENTS --> PUB["Publishing Desk<br/>html · pdf · pptx · site · md · json · charts"]
  CHAT --> GW

  TOOLS --> NET(["Public web<br/>robots.txt and site terms honoured"])
  GW --> PROV(["Free model tiers<br/>Groq · Gemini · OpenRouter · Mistral ·<br/>NVIDIA NIM · Cloudflare · Cerebras · Ollama"])
  PUB --> QD[("Qdrant<br/>chat index")]
  QD --> CHAT

  AGENTS <-.-> DB[("SQLite<br/>caches · runs · facts · quotas ·<br/>reputations · playbooks")]
```

Which checks run where: chat questions get every layer on every surface. Research requests get every layer in the
hosted app; the local dashboard and MCP server clean them and reject instruction-like text; the CLI is the operator's
own terminal and skips request checks. Web pages are screened separately by the Security Officer on every run.

| Component | Code | What it does |
|---|---|---|
| CLI, dashboard, MCP server | [`cli.py`](../src/resurge/cli.py), [`web/app.py`](../src/resurge/web/app.py), [`mcp_server.py`](../src/resurge/mcp_server.py) | Start runs, follow progress, open reports, ask questions |
| Website | [`frontend/`](../frontend) (React, Next.js) | Landing page and the app: sign-up, dashboard, progress, account, admin |
| Hosted web app | [`web/hosted.py`](../src/resurge/web/hosted.py), [`web/accounts.py`](../src/resurge/web/accounts.py) | JSON API, serves the website, accounts, sessions, daily limits, run queue, admin and audit log |
| Input checks | [`input_guard.py`](../src/resurge/input_guard.py), [`guardrails.py`](../src/resurge/guardrails.py) | Shape and rule checks, Prompt Guard 2, acceptable-use policy model, output checks |
| Orchestrator | [`pipeline/orchestrator.py`](../src/resurge/pipeline/orchestrator.py), [`pipeline/context.py`](../src/resurge/pipeline/context.py) | Runs the 12 stages; the run context is the shared blackboard every agent reads and writes |
| Agents | [`agents/`](../src/resurge/agents/) (org chart in [`org.py`](../src/resurge/agents/org.py)) | One module per department; each agent has a job title, a mission and a model role |
| Validation | [`agents/quality.py`](../src/resurge/agents/quality.py), [`text.py`](../src/resurge/text.py) | Quote grounding, citation integrity, confidence scores, QA gates, BM25 |
| Model gateway | [`llm/gateway.py`](../src/resurge/llm/gateway.py), [`llm/ratelimit.py`](../src/resurge/llm/ratelimit.py), [`llm/registry.py`](../src/resurge/llm/registry.py), [`config/models.yaml`](../config/models.yaml) | Picks a model per call, enforces quotas, fails over, repairs JSON, caches |
| Budget | [`budget.py`](../src/resurge/budget.py) | Per-run limits for model calls, searches and pages, with a reserve for analysis |
| Search | [`tools/search/`](../src/resurge/tools/search/) | Parallel MCP, Hacker News, Wikipedia, GitHub, plus Tavily, Exa, Jina, Serper, Brave, SearXNG when keyed |
| Fetching and compliance | [`tools/fetch.py`](../src/resurge/tools/fetch.py), [`tools/compliance.py`](../src/resurge/tools/compliance.py), [`tools/http.py`](../src/resurge/tools/http.py) | robots.txt, site-terms list, per-site throttle, circuit breaker, private-address blocking |
| Web-content security | [`security.py`](../src/resurge/security.py), [`agents/security_officer.py`](../src/resurge/agents/security_officer.py) | Hidden-text removal, injection quarantine, nonce fences, PII redaction |
| Outputs | [`outputs/`](../src/resurge/outputs/) | HTML report, PDF, PowerPoint, report site, charts, Markdown, JSON, Sheets, alerts |
| Chat | [`rag/`](../src/resurge/rag/) | Chunking, local embeddings, Qdrant, hybrid retrieval, cited answers |
| Storage | [`storage.py`](../src/resurge/storage.py), [`cache.py`](../src/resurge/cache.py) | One SQLite file for caches, runs, events, quotas, reputations, knowledge base and playbooks |

## A run, start to finish

```mermaid
flowchart TD
  REQ(["Request: a company, optional competitors and questions, a depth"]) --> S1
  S1["1 · Intake<br/>Chief of Staff writes the brief"] --> S2
  S2["2 · Competitive set<br/>Market Scout finds the rivals,<br/>or resolves the websites of those given"] --> S3
  S3["3 · Knowledge reuse<br/>verified facts under 3 days old skip their questions"] --> S4
  S4["4 · Research plan<br/>Strategist: topics → questions → search queries"] --> S5
  S5["5 · Research<br/>search · fetch · extract, per company<br/>(details below)"] --> V1
  V1{"Is the quote really<br/>in the source?"}
  V1 -- no --> RJ["rejected"]
  V1 -- yes --> V2["Fact Checker<br/>a different model family re-reads the source"]
  V2 --> V3["Confidence score<br/>verdict × source reliability<br/>+ corroboration − staleness"]
  V3 --> GAP{"Topics with<br/>no facts?"}
  GAP -- "yes, budget left" --> S6["6 · Gap closing<br/>follow-up questions"] --> S5
  GAP -- no --> S7["7 · Analysis<br/>profiles · feature matrix · head-to-head ·<br/>positioning · market · action plan"]
  S7 --> QA{"8 · QA Reviewer<br/>passes the rubric?"}
  QA -- "revise, with the issues" --> S7
  QA -- pass --> S9["9 · Risk register"]
  S9 --> S10["10 · Battlecards and executive summary"]
  S10 --> S11["11 · Output guardrails, then publish<br/>every format + chat index"]
  S11 --> S12["12 · Retrospective<br/>lessons · model and source reputations"]
  S12 -. "makes the next run better" .-> S4
```

| Stage | Who | Model or code | Output |
|---|---|---|---|
| 1 Intake | Chief of Staff | model | Brief: industry, official website, focus areas |
| 2 Competitive set | Market Scout | model + search | Up to 3, 4 or 6 competitors by depth (or up to 8 you name), each grounded in search results |
| 3 Knowledge reuse | Knowledge base | code | Recent verified facts, so their questions are skipped |
| 4 Research plan | Research Strategist | model | Questions and queries per company and topic (6 topics at `quick`, 9 at `standard`, 11 at `deep`) |
| 5 Research | Research Lead + 4 specialists, Security Officer | model + code | Facts with a source URL and a verbatim quote |
| 5 Verification | Validation Engine, Fact Checker | code, then model | Grounded, verified facts with confidence scores |
| 6 Gap closing | Research Strategist | model | Follow-up questions for empty topics (1 round at `standard`, 2 at `deep`) |
| 7 Analysis | Data Analyst, Strategy Analyst | model | Profiles, pricing tables, feature matrix, head-to-head, positioning map, market trends, action plan |
| 8 QA | QA Reviewer | model (different family from the writer) | Rubric score; sends weak analysis back with concrete issues (1 revision at `standard`, 2 at `deep`) |
| 9 Risk | Chief Risk Officer | model + code | Risk register, including research-quality and compliance risks |
| 10 Enablement | Enablement Writer, CEO | model | Sales battlecards, executive summary |
| 11 Publishing | Publishing Desk, Visualization Designer | code | HTML report, PDF, deck, report site, Markdown, JSON, charts, chat index |
| 12 Learning | Retrospective Analyst, Playbook Curator | model + code | New lessons for the roles that failed, updated reputations |

## Inside one research round

Each company gets its own round:

```mermaid
flowchart TD
  Q["Questions for one company"] --> SR["Search router<br/>several backends, rank fusion, cached 24 h"]
  SR --> PICK["Pick the best URLs, official site first"]
  PICK --> OKF{"May we fetch it?<br/>site terms · robots.txt · public address"}
  OKF -- no --> SNIP["Search snippet only"]
  OKF -- yes --> GET["Polite fetch<br/>one request at a time per site · Retry-After ·<br/>circuit breaker · cached 3 days"]
  GET --> SEC["Security Officer<br/>strip hidden text · quarantine instructions ·<br/>redact emails and phones · Prompt Guard"]
  SNIP --> SEC
  SEC --> BM["BM25 picks the top chunks per question"]
  BM --> EX["Specialist researcher<br/>sources in a fenced block → claim + exact quote"]
  EX --> GR{"Quote overlap<br/>with the chunk"}
  GR -- "0.6 or more" --> FC["Fact Checker, 8 facts per call"]
  GR -- "0.45 to 0.6" --> FC
  GR -- "below 0.45" --> X["rejected"]
```

Near matches (0.45 to 0.6) reach the Fact Checker with a confidence penalty. A page that only renders with
JavaScript may be read through Jina Reader or Parallel, but only when the site itself has not refused.

## How a model is picked for each call

```mermaid
flowchart TD
  A["Agent needs a model for its role<br/>(e.g. fact_checker)"] --> C{"Same prompt<br/>answered before?"}
  C -- yes --> HIT["Cached answer, no quota used"]
  C -- no --> L["Candidates: the role's preferred models<br/>plus the rest of the free pool,<br/>minus disabled models and avoided families"]
  L --> T["Order by Thompson sampling<br/>on each model's track record for this role"]
  T --> Q{"Quota free now?<br/>requests and tokens,<br/>per minute and per day"}
  Q -- "wait over 25 s" --> NEXT["Next candidate"]
  Q -- yes --> CALL["Call the provider"]
  CALL -- "busy · timeout · quota" --> LEARN["Record the failure, honour retry-after,<br/>disable models that are gone"] --> NEXT
  CALL -- ok --> P{"Valid JSON that<br/>passes the validators?"}
  P -- no --> FIX["Repair, ask the same model to fix it,<br/>then try a different model"] --> NEXT
  P -- yes --> R["Use it and update the model's stats"]
  NEXT --> Q
```

The validators include a banned-jargon list, which is why a run log can show *invalid output (replace jargon with
specifics: seamless)*: the answer was rejected and rewritten, not lost.

## The agents

| Department | Agent | Job | Default free models (first choice → fallbacks) |
|---|---|---|---|
| Executive Office | **CEO** | Narrative, executive summary, sign-off | Gemini 3.8 Flash → Nemotron-3-Ultra 550B → gpt-oss-120b |
| | **Chief of Staff** | Turns the request into a precise brief | gpt-oss-20b (Groq) → Gemini 3.1 Flash-Lite → Gemma-4-26B |
| Finance | **CFO** *(code)* | Per-run budgets, quota reserve, cost accounting | — 0 tokens |
| Infrastructure | **CTO / Model Router** *(code)* | Quota-aware routing, failover, learning | — 0 tokens |
| Strategy & Planning | **Market Scout** | Discovers and qualifies the competitive set | gpt-oss-120b → Gemini Flash-Lite → Qwen3.8 |
| | **Research Strategist** | Dimensions, questions, queries, sources; closes gaps | Qwen3.8-27B → gpt-oss-120b |
| Research | **Product & Pricing Researcher** | Products, features, pricing, positioning | Gemini 3.5 Flash-Lite → Gemma-4-31B |
| | **Market & Funding Researcher** | Funding, news, go-to-market, hiring | Gemma-4-31B → Gemini 3.1 Flash-Lite |
| | **Voice-of-Customer Researcher** | Reviews, community sentiment, segments | gpt-oss-20b → Gemma-4-26B |
| | **Technology Researcher** | Integrations, APIs, AI, open source | Mistral Small → Gemini 3.1 Flash-Lite |
| Quality Assurance | **Validation Engine** *(code)* | Schema, citation-integrity, quote-grounding, coverage gates | — 0 tokens |
| | **Fact Checker** | Claim ⇄ source entailment, **different model family** | Qwen3.8-27B → Gemma-4-31B → gpt-oss-120b (NIM) |
| | **QA Reviewer** | Rubric scoring, sends work back for revision | Nemotron-3-Ultra → gpt-oss-120b |
| Analysis | **Data Analyst** | Profiles and pricing tables from verified facts | Gemini 3.5 Flash-Lite → gpt-oss-120b |
| | **Strategy Analyst** | Feature matrix, head-to-head vs the client, positioning, market changes, gaps, action plan | gpt-oss-120b → Gemini 3.6 Flash → Nemotron |
| Risk Office | **Chief Risk Officer** | Competitive, market, research-quality, compliance risks | Nemotron-3-Ultra → Qwen3.8 → gpt-oss-120b |
| | **Security & Compliance Officer** *(code + optional ML)* | Prompt-injection screening, PII redaction, robots.txt and site-terms enforcement | deterministic; Llama Prompt Guard 2 (Groq, free) for borderline text |
| Design & Comms | **Enablement Writer** | Sales battlecards and objection handling | Mistral Medium → Gemini 3.6 Flash → gpt-oss-120b |
| | **Visualization Designer** *(code)* | Charts | — |
| | **Publishing Desk** *(code)* | HTML report, PDF, deck, Markdown, JSON, Sheets, alerts | — |
| Learning & Development | **Retrospective Analyst** | Turns failures into lessons (ACE reflector) | gpt-oss-120b → Nemotron |
| | **Playbook Curator** *(code)* | Dedupes, caps, prunes lessons; updates reputations | — |

`resurge org` prints this table with the models each role would route to **right now**, given your keys, quotas and
learned performance. Routing lives in [`config/models.yaml`](../config/models.yaml). Every department prefers a
different model family, and the router adds the rest of the free pool plus local fallbacks.

## Validation

1. **No source, no claim.** Researchers must return `evidence_id` plus a **verbatim quote** for every fact.
2. **Quote grounding (code, 0 tokens).** The quote must literally appear in the cited chunk (shingle overlap ≥ 0.6).
   Near-matches (0.45–0.6, typical of paraphrasing small models) go to the verifier with a confidence penalty, and
   anything lower is rejected before any LLM sees it. This catches invented numbers and wrong-company claims cheaply.
3. **Cross-family fact checking.** The Fact Checker uses a different model family than the extractor (e.g. Gemma
   extracts, Qwen verifies), so the two are unlikely to share the same mistake. Verdicts are
   supported / partial / unsupported / contradicted, and small corrections are applied.
4. **Entity disambiguation.** Each company's identity comes from its official homepage title and official redirects
   (e.g. notion.so ↔ notion.com). Researchers and the Fact Checker see it, so a namesake (e.g. *coda.co*, a payments
   company, vs *coda.io*) is rejected. A deterministic namesake-domain penalty backs this up. Mis-filed facts are
   re-filed to the right dimension.
5. **Confidence model.** Confidence combines the verdict, the **learned** reliability of the source domain,
   corroboration from other independent domains, and a staleness penalty for fast-changing data (pricing, funding,
   headcount). It also flags **numeric conflicts**, such as two prices for one plan.
6. **Citation integrity.** Analysts see only verified facts. Unknown fact IDs are stripped, uncited insights are
   labelled *inferred*, and **any price that no cited fact contains is removed from charts**.
7. **QA gates with revision loops.** A rubric reviewer scores evidence, specificity, consistency, coverage and
   actionability. A *revise* verdict sends the analysis back with the concrete issues.
8. **Honest risk register.** Coverage gaps, single-source findings, stale data, conflicts, high rejection rates and
   the data-use terms of the free tiers used are reported as research-quality and compliance risks.

## Guardrails

Requests and chat questions pass rules, Llama Prompt Guard 2 and gpt-oss-safeguard-20b with a written acceptable-use
policy (no crime, espionage, private people, fake reviews). Answers and reports are checked for prompt leaks (canary
token), API keys, personal data and instruction residue.

```mermaid
flowchart TD
  IN["Research request or chat question"] --> L1{"1 · Rules<br/>instant, free"}
  L1 -- "instruction-like" --> NO["Refused, with a plain-English reason"]
  L1 -- ok --> L2{"2 · Llama Prompt Guard 2<br/>jailbreak score 0.9 or more?"}
  L2 -- yes --> NO
  L2 -- no --> L3{"3 · gpt-oss-safeguard-20b<br/>written acceptable-use policy"}
  L3 -- "injection · harm · espionage ·<br/>privacy · deception" --> NO
  L3 -- allowed --> YES["Research runs or the answer is written"]
  YES --> OUTC["Output check<br/>canary · API keys · personal data ·<br/>instruction text · script links"]
  OUTC --> SAFE["Shown to the user"]
  NO -. "hosted app" .-> STRIKE["Strike in the audit log<br/>5 in 24 h pauses the account for the day"]
```

`RESURGE_GUARDRAILS`: `strict` (hosted default) refuses when the policy model cannot run, `on` (local default) falls
back to the rules, `off` skips the model layers. A busy safety model hands over to another free model after 2 seconds.
Code: [`guardrails.py`](../src/resurge/guardrails.py). Crawling and data policy: [COMPLIANCE.md](COMPLIANCE.md).

## Self-improvement

```mermaid
flowchart LR
  RUN["A run"] --> SIG["Validated signals<br/>grounding · fact-check verdicts ·<br/>QA scores · errors"]
  SIG --> M["Model reputation"] --> R1["Router sends more<br/>work to good models"]
  SIG --> S["Source reliability"] --> R2["Confidence scores"]
  SIG --> Q["Search strategy"] --> R3["site: queries for<br/>sources that worked"]
  SIG --> P["Playbook lessons"] --> R4["Added to the failing<br/>agent's prompt"]
  R1 & R2 & R3 & R4 --> NEXT["Next run"]
```

| Loop | Signal (validated, not self-graded) | Effect next run |
|---|---|---|
| Model reputation | per (role, model): JSON validity, share of facts that survive grounding and verification, QA scores | Router uses Thompson sampling over these stats, so better models get more traffic and new ones still get explored |
| Source reputation | per domain: supported vs unsupported verdicts | Bayesian reliability feeds confidence scores |
| Research strategy | which domains yield verified facts for each dimension | Strategist adds `site:` queries for proven sources |
| Playbooks (ACE, ICLR 2026) | Reflector reads concrete failures (rejected facts, QA issues, gaps, errors) | Curator stores ≤30-word lessons **only for roles that actually failed this run**, deduplicated and capped at 12 per role. Helpful/harmful counters are tied to run-quality deltas. Retirement is **evidence-gated**: an LLM's opinion alone cannot delete a lesson with a good track record |

`resurge learn` shows the playbook, the model leaderboard, source reputation and the quality trend. The dashboard has
the same data. ACE's known weakness is learning from unreliable signals; here the validators supply the outcome
signal.

## Staying free

| Provider | Free tier (checked Nov 2026; limits change, and the router also learns them live) | Best used for |
|---|---|---|
| Groq | No card. 30 RPM / 1K RPD / 8K TPM per chat model (gpt-oss-120b, gpt-oss-20b, Qwen3.8) | Fast short calls: planning, verification, QA |
| Google AI Studio | No card. Gemini 3.x Flash ~5 RPM / ~20 RPD; Flash-Lite ~15 RPM / ~500 RPD; Gemma 4 ~15 RPM / ~1.5K RPD; 1M context | Long-context extraction (Flash-Lite, Gemma), top-quality writing (3.8 Flash, sparingly) |
| OpenRouter `:free` | No card. 20 RPM, **50 requests/day shared** (1K/day after a one-time $10 top-up) | Rare high-value reasoning (Nemotron-3-Ultra 550B) |
| Mistral | Free plan, restrictive per-model limits | Writing, extraction |
| NVIDIA NIM | Free developer access, ~40 RPM | Extra capacity for gpt-oss-120b, Llama-3.3-70B |
| Cloudflare Workers AI | 10K neurons/day | Small overflow |
| Cerebras | Trial credits | Very fast overflow |
| Ollama (local) | Unlimited, private | Last-resort fallback; `--local` for sensitive work |

Gemini's free tier and Mistral's free plan may use prompts for training; `--private` excludes such providers. The
research behind these choices, with sources: [RESEARCH.md](RESEARCH.md).

## Cost and performance

1. **Deterministic where possible.** Validation, grounding, confidence, routing, budgeting, charts and publishing are
   code, not LLM calls.
2. **Retrieval before reasoning.** Pages are chunked and BM25-ranked locally, and each researcher sees only the top
   chunks for its questions (≈3.5K tokens instead of 50K+ per page set).
3. **Tiered models per role.** Small models do classification, mid-size models do extraction, and the scarce frontier
   free quota goes to synthesis.
4. **Quota-aware routing.** Sliding-window RPM/TPM and persistent RPD/TPD counters per model and per provider. The day
   boundary follows each provider's reset timezone (Gemini uses Pacific time).
5. **Never idle on one provider.** If a slot is more than 25 s away, the router moves to the next candidate; it only
   waits when every model is busy.
6. **Learns provider quirks.** It disables dead or forbidden models, drops unsupported `response_format`, system
   prompts and `reasoning_effort` per model, persistently, and reads `retry-after` and `x-ratelimit-*` headers.
7. **Three caches.** LLM responses (keyed by role and prompt, model-agnostic), search results (24 h) and pages
   (3 days), all in SQLite.
8. **Knowledge-base reuse.** Verified facts younger than N days (`--reuse-days`, default 3) are reused and those
   questions are skipped, which makes incremental monitoring cheap.
9. **Compact structured output.** A TypeScript-like schema hint replaces verbose JSON Schema, followed by forgiving
   parsing, repair, self-repair by the same model, and finally a different model.
10. **Batching.** 8 facts per verification call and 2 battlecards per call.
11. **Stable prompt prefixes.** Identity, standards and playbook come first so provider prompt caches hit; Groq does
    not count cached tokens toward its limits.
12. **CFO budgets.** `quick`/`standard`/`deep` presets, a reserve that keeps analysis and reporting alive, and honest
    degradation (e.g. "grounded but not LLM-verified") instead of failure.
13. **Cost transparency.** Every run reports tokens and what they would have cost on a paid frontier API ($3/$15 per
    M tokens, configurable). Actual cost is $0.

A slow provider can hold a call until the per-call timeout (`RESURGE_LLM_TIMEOUT_S`, default 180 s); lowering it
makes runs faster when a provider is overloaded.

## Hosted app request flow

```mermaid
sequenceDiagram
  actor U as User
  participant W as Web app
  participant G as Guardrails
  participant Q as Run queue
  participant E as Engine
  U->>W: Sign up or log in
  W-->>U: Session cookie (random token, stored hashed)
  U->>W: New analysis (company or website, competitors, questions)
  W->>W: Origin check, paused?, shape and rule checks, daily limits
  W->>G: Request text
  G-->>W: Allowed, or refused with a category
  alt refused
    W-->>U: Plain-English reason (strike in the audit log)
  else allowed
    W->>Q: Add job
    W-->>U: Progress page
    Q->>E: Run the 12 stages (one run at a time by default)
    E-->>W: Report site and chat index
    U->>W: Open report (owner only)
    U->>W: Ask a question
    W->>G: Question
    W-->>U: Cited answer, output-checked
  end
```

The website is a React app built with Next.js ([`frontend/`](../frontend)) and exported as static files that the same
Python process serves next to its JSON API. One origin carries the session cookie, the Origin checks and a strict
Content-Security-Policy: each page's few inline scripts are allowed by their SHA-256 hash, and nothing else inline
runs. A web address typed as the company (`https://www.linear.app/pricing`) keeps only the company's main site
(`linear.app`); logins, ports, IP addresses and internal names are refused. For live editing of the website, run
`npm run dev` next to `RESURGE_ALLOWED_ORIGINS=http://localhost:3000 resurge serve`. Hosting: [DEPLOY.md](DEPLOY.md).

## Example run

`resurge run "Linear" --depth quick` with free Groq, Gemini and OpenRouter keys: 4 companies, 39 searches, 24 pages,
38 successful model calls, 161K tokens, quality 90.3/100, $0.00 (about $1.09 at paid frontier-API prices).

```mermaid
xychart-beta
  title "From sources to verified facts"
  x-axis ["Evidence chunks", "Facts extracted", "Quote grounded", "Verified"]
  y-axis "Count" 0 --> 220
  bar [209, 75, 75, 69]
```

```mermaid
pie showData
  title Why 27 calls switched to another model
  "Gemini busy (HTTP 503)" : 21
  "Gemini timed out" : 3
  "Gemini daily free quota used up" : 2
  "Groq prompt too large for the token limit" : 1
```

Successful calls by provider: Groq 14, OpenRouter 12, Gemini 12. A failover is a call that moved to the next model;
none of them lost work.
