# Deploying the Resurge web app

People open a link, read what Resurge is on the landing page, create an account, type a company or its website and
get the report. Nothing to install on their side.

```
Browser ──HTTPS──> Caddy or Cloudflare Tunnel ──> resurge serve (Docker: website + API) ──> free LLM APIs, public web
                                                       │
                                                       └── /data volume: accounts, research database, reports, chat index
```

## What the app enforces (because everyone shares your free API keys)

| Setting (`.env`) | Default | Purpose |
|---|---|---|
| `RESURGE_SIGNUP_OPEN` | `1` | Anyone can create an account; set `0` to close sign-up |
| `RESURGE_RUNS_PER_USER_DAY` | 2 | Analyses per account per day |
| `RESURGE_RUNS_PER_IP_DAY` | 4 | Stops one person creating many accounts |
| `RESURGE_RUNS_PER_DAY` | 20 | Site-wide daily cap |
| `RESURGE_MAX_CONCURRENT_RUNS` | 1 | Runs at the same time; the rest wait in line |
| `RESURGE_CHAT_PER_USER_DAY` | 40 | Chat questions per account per day |
| `RESURGE_ALLOWED_DEPTHS` | `quick,standard` | Depths users may choose |
| `RESURGE_ADMIN_EMAILS` | none | Accounts that see `/admin` and have no limits |
| `TURNSTILE_SITE_KEY`, `TURNSTILE_SECRET_KEY` | off | Free Cloudflare bot check on sign-up (recommended with open sign-up) |
| `RESURGE_PUBLIC_URL` | none | Your https address; turns on secure cookies |
| `RESURGE_GUARDRAILS` | `strict` | `strict` refuses a request when the policy check can't run; `on` lets the rule layer decide alone; `off` skips the model checks |
| `RESURGE_GUARD_STRIKES` | 5 | Blocked requests in 24 hours before an account is paused for the rest of the day |
| `RESURGE_FRONTEND_DIR` | `frontend/out` | The built website; the Docker image sets it for you |
| `RESURGE_ALLOWED_ORIGINS` | none | Extra origins allowed to post, only for the Next.js dev server (`http://localhost:3000`) |

Security built in:
- **Log-in:** scrypt password hashing (Unicode-normalised); per-account lockout after 5 failures (15, 30, then 60
  minutes, stored in the database); per-IP and per-email throttling; identical answers and timing for unknown
  accounts; sign-up that doesn't reveal which emails exist; breached-password check against Have I Been Pwned using
  k-anonymity (only a 5-character hash prefix leaves the server; `RESURGE_PWNED_CHECK=0` turns it off).
- **Sessions:** random tokens stored only as hashes, rotated at log-in, 3-day idle and 14-day absolute expiry,
  `__Host-` HttpOnly SameSite cookies over HTTPS, password change and "log out everywhere" revoke every session.
- **Abuse:** one account per inbox (plus-aliases and Gmail dots count as the same address), throwaway email domains
  refused (`RESURGE_BLOCKED_EMAIL_DOMAINS` adds more), honeypot field, optional Turnstile, request size limit, daily caps.
- **Requests:** Origin check on every form and API call, strict Content-Security-Policy on app and report pages,
  no caching of private pages, reports visible only to their owner, provider error text never shown to users.
- **Prompt injection:** company names, competitors and questions are shape-checked and scanned before any model sees
  them (override phrases, role tags, code, off-task requests such as "write a phishing email"); questions reach the
  agents as a fenced data block; chat questions and the browser-supplied chat history are screened the same way, and
  instruction-like input is refused without a model call.
- **Guardrails (model-based safety checks):** after the cheap checks and daily limits, every research request and chat
  question goes through two free models on Groq: Meta's Llama Prompt Guard 2, a classifier for jailbreaks and
  prompt injection, and OpenAI's gpt-oss-safeguard-20b, which reasons over Resurge's written acceptable-use policy
  (no help with crime, corporate espionage such as internal documents or bribing staff, profiling private people,
  fake reviews or smears). When the safeguard model is busy, other free models apply the same policy; a busy model
  hands over after 2 seconds and the whole check gives up after 20. Chat answers carry a random canary token, so an
  answer that leaks the instructions is replaced, and answers and reports are cleaned of API keys, personal data,
  leftover instruction text and script links before anyone sees them. Accounts that are blocked 5 times in 24 hours
  are paused for the day; refusals say why in plain words, but which check fired stays in the audit log.
- **Server-side request forgery:** page fetches (and every redirect) may only reach public internet addresses: cloud
  metadata (169.254.169.254), localhost, private networks and unusual ports are refused.
- **Accountability:** account deletion removes the user's analyses and reports; admins can disable accounts; every
  security event (log-ins, failures, lockouts, rejected input, guardrail blocks, blocked chat) is in the audit log on
  `/admin`. Size the daily caps to your free quotas: one quick
analysis uses about 25-45 model calls.

## Option 1 (recommended): Oracle Cloud Always Free server

Always on, a real disk, and enough memory. Oracle asks for a card to verify the account; Always Free resources are not
charged. Caveats: free ARM capacity is sometimes unavailable in a region (retry or pick another), and Oracle may
reclaim an Always Free instance it considers idle (see Oracle's Always Free documentation for the current rules).

1. **Create the server.** Pick the home region closest to your visitors when you sign up (it can't be changed later;
   in India: Mumbai or Hyderabad). Oracle Cloud console: Compute > Instances > Create. Image: Ubuntu 24.04. Shape:
   Ampere (VM.Standard.A1.Flex) with 2 OCPU and 12 GB memory, which fits the free allowance (reported as halved to
   2 OCPU / 12 GB for free accounts in June 2026). Add your SSH key.
2. **Install Docker** (on the server):
   ```bash
   sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
   sudo usermod -aG docker $USER && newgrp docker
   ```
3. **Get the code and settings:**
   ```bash
   git clone <your repository URL> resurge && cd resurge
   cp .env.example .env && nano .env   # add at least one free LLM key; set the hosted settings above
   ```
   Keys go only into `.env` on the server. Never commit `.env`.
4. **Pick how people reach it over HTTPS:**
   - **Caddy (free, needs a hostname):** get a free subdomain, for example at duckdns.org, pointed at the server's
     public IP. In Oracle's VCN security list allow inbound TCP 80 and 443, and on the server
     `sudo iptables -I INPUT -p tcp -m multiport --dports 80,443 -j ACCEPT`. Set `DOMAIN=` and
     `RESURGE_PUBLIC_URL=https://<your domain>` in `.env`, then:
     ```bash
     docker compose --profile caddy up -d --build
     ```
   - **Cloudflare Tunnel (no open ports; needs a domain on Cloudflare):** in Cloudflare Zero Trust create a tunnel,
     add a public hostname whose service is `http://resurge:8000`, copy the token into `CLOUDFLARE_TUNNEL_TOKEN=`,
     set `RESURGE_PUBLIC_URL`, then `docker compose --profile tunnel up -d --build`.
5. **Make yourself admin:** set `RESURGE_ADMIN_EMAILS=you@example.com` in `.env`, run `docker compose up -d`, then sign
   up with that email. `/admin` shows accounts, today's usage and the queue.
6. **Turn on the bot check (recommended):** Cloudflare dashboard > Turnstile > add a widget for your hostname, put
   the two keys in `.env`, `docker compose up -d`.

**Updates:** `git pull && docker compose up -d --build`. **Backups:** the `resurge-data` volume holds everything;
`docker run --rm -v resurge_resurge-data:/data -v $PWD:/backup alpine tar czf /backup/resurge-data.tgz /data`.
**Logs:** `docker compose logs -f resurge`.

## Other hosts

- **Any VPS or home server with Docker:** same steps as Oracle from step 2.
- **Hugging Face Spaces (Docker):** Docker Spaces now appear to need a paid plan, the free disk is wiped on restart
  (accounts and reports would be lost) and Spaces sleep when unused. Check the current terms before relying on it.
- **Render free tier:** not suitable: about 512 MB of memory, a fraction of a CPU, sleeps after 15 minutes without
  traffic and wipes its disk on restart, while an analysis runs for 10-30 minutes.

## Trying it locally first

```bash
pip install -e ".[web,rag]"
cd frontend && npm install && npm run build && cd ..   # the website, once (Node 20+)
RESURGE_ADMIN_EMAILS=you@example.com resurge serve --host 127.0.0.1 --port 8000
```
Open http://127.0.0.1:8000, sign up, and start an analysis. After changing the website, run `npm run build` again
(the server picks up the new files without a restart), or work with live reload: `npm run dev` in `frontend/` and
`RESURGE_ALLOWED_ORIGINS=http://localhost:3000 resurge serve`, then open http://localhost:3000. `resurge ui` is still available for the single-user local
dashboard. With only a local Ollama model and no free cloud key, the policy check has no model to run on, so add
`RESURGE_GUARDRAILS=on` for local trials (the hosted default, `strict`, would refuse every request).
