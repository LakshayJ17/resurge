# Deploying the Resurge web app

People open a link, read what Resurge is on the landing page, create an account, type a company or its website and
get the report. Nothing to install on their side.

```
Browser --HTTPS--> Caddy (or a Cloudflare Tunnel) --> resurge serve (Docker: website + API) --> free LLM APIs, public web
                                                       |
                                                       +-- /data volume: accounts, research database, reports, chat index
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
| `RESURGE_ADMIN_EMAILS` | none | Addresses that may be admins (see `/admin`, no limits). The account itself is created with `resurge admin EMAIL` on the server; web sign-up refuses these addresses |
| `RESURGE_CAPTCHA` | `on` | Self-hosted proof-of-work bot check on sign-up and log-in (no third party); `off` disables it |
| `RESURGE_CAPTCHA_MAX` | 100000 | Puzzle size: the browser tries about half this many hashes (well under a second) |
| `RESURGE_REQUEST_BURST`, `RESURGE_REQUEST_RATE` | 120, 2 | Requests per network: a burst of 120, then 2 a second (static files and `/healthz` are exempt); `RESURGE_REQUEST_BURST=0` turns the limit off |
| `TURNSTILE_SITE_KEY`, `TURNSTILE_SECRET_KEY` | off | Cloudflare's free bot check on sign-up, on top of the proof of work |
| `RESURGE_PUBLIC_URL` | none | Your full https address (with `https://`); turns on secure cookies, and only this origin may post and only its host name is answered |
| `RESURGE_ALLOWED_HOSTS` | none | Extra host names the server answers to (besides localhost and the public URL), e.g. a LAN address for testing; other Host headers get 421 |
| `RESURGE_TRUST_PROXY` | `0` | `1` behind Caddy (reads the last `X-Forwarded-For` entry), `cloudflare` behind a Cloudflare Tunnel (reads `CF-Connecting-IP`); docker-compose sets `1` |
| `RESURGE_GUARDRAILS` | `strict` | `strict` refuses a request when the policy check can't run; `on` lets the rule layer decide alone; `off` skips the model checks |
| `RESURGE_GUARD_STRIKES` | 5 | Blocked requests in 24 hours before an account is paused for the rest of the day |
| `RESURGE_FRONTEND_DIR` | `frontend/out` | The built website; the Docker image sets it for you |
| `RESURGE_ALLOWED_ORIGINS` | none | Extra origins allowed to post, only for the Next.js dev server (`http://localhost:3000`) |

Security built in:
- **Log-in:** scrypt password hashing (Unicode-normalised, run off the request loop); lockout after 5 failures from
  one network (15, 30, then 60 minutes), so a stranger guessing locks only themselves out, plus an account-wide pause
  after 50 failures in a day from anywhere; both stored in the database; per-IP and per-email throttling; identical
  answers and timing for unknown, wrong-password and locked accounts; breached-password check against Have I Been
  Pwned using k-anonymity (only a 5-character hash prefix leaves the server; `RESURGE_PWNED_CHECK=0` turns it off).
  Known limit: without email verification, sign-up says when an address already has an account (rate-limited to 5
  tries per network per hour).
- **Admins:** only made on the server (`resurge admin EMAIL`), and only while the address is in
  `RESURGE_ADMIN_EMAILS`; removing it from the list removes the rights at the next restart. The `/admin` page is a
  404 for everyone else (its API refuses them too), and admin actions are logged with who did them: opening the
  panel, opening someone else's analysis or report, disabling or enabling an account.
- **Bot check:** sign-up and log-in send a solved proof-of-work puzzle (the scheme ALTCHA uses): the server signs a
  random puzzle, the browser solves it while the person types, and each solution works once within 10 minutes.
  It costs a script CPU time per attempt but proves nothing about being human, so it works together with the limits
  below rather than replacing them.
- **Sessions:** random tokens stored only as hashes, rotated at log-in, 3-day idle and 14-day absolute expiry,
  `__Host-` HttpOnly SameSite cookies over HTTPS, password change and "log out everywhere" revoke every session.
- **Abuse:** one account per inbox (plus-aliases and Gmail dots count as the same address), throwaway email domains
  refused (`RESURGE_BLOCKED_EMAIL_DOMAINS` adds more), honeypot field, optional Turnstile, a 64 KB request limit that
  also holds for chunked uploads, daily caps checked and reserved in one step (parallel requests can't slip past them)
  and counted against the hashed address and network, so deleting an account and signing up again doesn't reset them.
- **Requests:** Origin check on every form and API call (no CORS headers, so other sites can't read the API), a
  general per-network request limit, chat questions limited to 6 a minute on top of the daily cap, strict
  Content-Security-Policy on app and report pages (`default-src 'none'` on API answers), HSTS over HTTPS,
  `Cross-Origin-Resource-Policy: same-origin`, no API docs and no server banner, no caching of private pages,
  reports visible only to their owner (any other id is a 404), provider error text never shown to users. SQL
  always takes values as parameters, and the two helpers that build column names accept only real columns.
- **Dependencies:** check for known vulnerabilities before an update with `uvx pip-audit` (Python) and `npm audit`
  in `frontend/`; GitHub's free "Dependabot alerts" (Settings > Code security) can also warn by email.
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
analysis uses about 25 to 45 model calls.

## 1. Get a server

Any Ubuntu 24.04 server with Docker works. Two free ones:

### Azure for Students (no card needed)

Sign up with a college email at [azure.microsoft.com/free/students](https://azure.microsoft.com/free/students). It
gives $100 of credit for 12 months plus 750 free hours a month of the B1s, B2ats v2 and B2pts v2 servers, which is
enough to run one all month. The disk and public IP draw a few dollars a month from the credit; check what's left
under Cost Management > Credits. When the offer ends, Azure stops the resources instead of charging you.

1. **Find your allowed regions.** Student subscriptions may only deploy to some regions: Portal > Policy >
   Assignments > "Allowed resource deployment regions" > Parameters. Pick the closest one; any other gives
   `RequestDisallowedByAzure`.
2. **Create the server:** Portal > Virtual machines > Create. Image Ubuntu Server 24.04 LTS (x64), size
   **B2ats v2** (2 vCPU, 1 GB), SSH public key authentication, inbound ports 22, 80 and 443, OS disk Standard SSD.
   Afterwards, in Networking, limit the SSH rule to your own IP address.

### Oracle Cloud Always Free

No time limit, and 2 OCPU / 12 GB of Arm (Ampere A1) capacity. Oracle asks for a credit card, or a Visa or
Mastercard debit card that works without a PIN, to verify the account; Always Free resources are not charged.
Two caveats from Oracle's
[Always Free rules](https://docs.oracle.com/en-us/iaas/Content/FreeTier/resourceref.htm): free Arm capacity is
sometimes unavailable ("out of host capacity": retry later), and an instance whose CPU, network and memory use all
stay under 20% for 7 days may be reclaimed, which a mostly idle web app can be, so keep backups. Oracle suggests
upgrading to Pay As You Go when capacity is short; it still doesn't charge for Always Free resources.

1. Sign up at [signup.oraclecloud.com](https://signup.oraclecloud.com). The home region can't be changed later,
   and Always Free servers can only be created there.
2. Compute > Instances > Create: image Canonical Ubuntu 24.04, shape VM.Standard.A1.Flex with 2 OCPU and 12 GB,
   and save the generated SSH key. The login user is `ubuntu`.
3. Open ports 80 and 443: in the instance's subnet, add ingress rules to the default security list for TCP 80 and
   443 from `0.0.0.0/0`, and on the server (Oracle's Ubuntu images block them by default):
   `sudo iptables -I INPUT -p tcp -m multiport --dports 80,443 -j ACCEPT && sudo netfilter-persistent save`.

## 2. Get a free web address

Caddy needs a hostname to get an HTTPS certificate from Let's Encrypt. At [duckdns.org](https://www.duckdns.org),
sign in, create a subdomain (for example `resurge-yourname.duckdns.org`) and set it to the server's public IP.

## 3. Set up the server

```bash
ssh ubuntu@<server-ip>        # azureuser@ on Azure
curl -fsSL https://raw.githubusercontent.com/LakshayJ17/resurge/web-app/deploy/setup-server.sh | bash
```

[`deploy/setup-server.sh`](../deploy/setup-server.sh) adds 4 GB of swap (a 1 GB server needs it), installs Docker
and git, and clones the code into `~/resurge`. Then create `~/resurge/.env` with at least one free LLM key (Groq
is required for the guardrails) and these settings:

```bash
DOMAIN=resurge-yourname.duckdns.org
RESURGE_PUBLIC_URL=https://resurge-yourname.duckdns.org
RESURGE_CONTACT=https://resurge-yourname.duckdns.org
RESURGE_ADMIN_EMAILS=you@example.com
```

Keys go only into `.env` on the server. Never commit it.

## 4. Start it

```bash
cd ~/resurge
docker compose --profile caddy pull
docker compose --profile caddy up -d
docker compose exec resurge resurge admin you@example.com    # asks for a password
```

The image comes from GitHub's container registry, built by
[`.github/workflows/docker-image.yml`](../.github/workflows/docker-image.yml) on every push. After the first build,
make the package public once (GitHub > your profile > Packages > resurge > Package settings > Change visibility); it
contains no keys. To build on the server instead, run `docker compose --profile caddy up -d --build`.

Open your address, log in as the admin, and start an analysis. Web sign-up refuses admin addresses, so nobody can
claim yours first. `/admin` shows accounts, today's usage and the queue. Caddy gets the certificate on the first
visit, which can take a minute.

**On a 1 GB server** keep `RESURGE_MAX_CONCURRENT_RUNS=1` (the default). Building a report and building its chat
index each peak at about 400 MB; the swap absorbs the rest.

**HTTPS without opening ports:** with a domain on Cloudflare, a Cloudflare Tunnel works instead of Caddy: create a
tunnel whose public hostname points at `http://resurge:8000`, set `CLOUDFLARE_TUNNEL_TOKEN=`, `RESURGE_PUBLIC_URL`
and `RESURGE_TRUST_PROXY=cloudflare`, then use `--profile tunnel`. Its free Turnstile bot check
(`TURNSTILE_SITE_KEY`, `TURNSTILE_SECRET_KEY`) can then protect sign-up.

## Running it

| Task | Command (in `~/resurge` on the server) |
|---|---|
| Deploy new code | `git pull && docker compose --profile caddy pull && docker compose --profile caddy up -d` |
| Logs | `docker compose logs -f resurge` |
| Status and memory | `docker compose ps` and `docker stats --no-stream` |
| Back up accounts and reports | `docker run --rm -v resurge_resurge-data:/data -v $PWD:/backup alpine tar czf /backup/resurge-data.tgz /data` |

Not suitable: **Render** and similar free tiers (about 512 MB, sleep after 15 minutes, disk wiped on restart, while an
analysis runs for 10 to 30 minutes) and **Hugging Face Spaces** (Docker Spaces need a paid plan; the free disk is
wiped on restart).

## Trying it locally first

```bash
pip install -e ".[web,rag]"
cd frontend && npm install && npm run build && cd ..   # the website, once (Node 20+)
RESURGE_ADMIN_EMAILS=you@example.com resurge admin you@example.com    # optional: an admin account
RESURGE_ADMIN_EMAILS=you@example.com resurge serve --host 127.0.0.1 --port 8000
```
Open http://127.0.0.1:8000, sign up (or log in as the admin), and start an analysis. To open it from another device on
your network, add that address's host to `RESURGE_ALLOWED_HOSTS` (the server answers only names it knows). After changing the website, run `npm run build` again
(the server picks up the new files without a restart), or work with live reload: `npm run dev` in `frontend/` and
`RESURGE_ALLOWED_ORIGINS=http://localhost:3000 resurge serve`, then open http://localhost:3000. `resurge ui` is still available for the single-user local
dashboard. With only a local Ollama model and no free cloud key, the policy check has no model to run on, so add
`RESURGE_GUARDRAILS=on` for local trials (the hosted default, `strict`, would refuse every request).
