# Chat with your research (RAG)

Ask plain-language questions about a finished run and get a short answer with numbered citations to verified
facts and their source URLs:

```
$ resurge chat
You > How much does Asana cost for 50 seats?
Starter: 50 × $13.49 = $674.50 per month, billed annually [1]. Advanced: 50 × $30.49 = $1,524.50 per month [2].
(Sources as of 2026-11-05.)
[1] Asana Starter: US$13.49 per user per month billed annually (Asana)  https://asana.com/pricing
[2] Asana Advanced: US$30.49 per user per month billed annually (Asana) https://asana.com/pricing
```

It costs **$0**: embeddings are computed locally, vectors live in Qdrant (embedded local mode or the free Qdrant
Cloud cluster), and answers use the same free-tier model router as the rest of Resurge.

## How it works

```
report.json -> chunks -> local embeddings -> Qdrant (one collection, filtered by run_id)
                                                        │
question -> (condense follow-up) -> dense top-k + BM25 -> RRF fusion -> fenced context -> free LLM -> validated answer
```

1. **Chunks** (`rag/chunks.py`). One chunk per *usable* fact (status supported/partial): competitor, dimension,
   claim, value, verbatim quote and `as_of` date. Derived chunks are added for the executive summary and key findings,
   recommendations, risks, battlecards (positioning, objections, pricing talk track), pricing tiers, feature-matrix
   cells, profiles, head-to-heads/SWOTs and the market view. Each one carries the IDs of the facts it cites.
   Rejected or unverified facts are never indexed. Chunks are rescanned for prompt injection: hostile text is
   dropped, and suspicious text is neutralised, flagged and down-weighted.
2. **Embeddings** (`rag/embed.py`). [fastembed](https://github.com/qdrant/fastembed) runs ONNX models on the CPU.
   The default is `BAAI/bge-small-en-v1.5` (384 dimensions, a ~67 MB quantised model). It is downloaded once from
   Hugging Face into `data/fastembed` and then runs offline. Indexing a 150-chunk run takes about 3 s on an M-series
   Mac.
3. **Store** (`rag/store.py`). There is one Qdrant collection per embedding model, so changing the model never mixes
   vector sizes. Every point's payload holds `run_id`, `target`, `competitor`, `dimension`, `kind`, `fact_ids`, `url`,
   `title`, `confidence`, `as_of`, the text and the quote. Every read is filtered by `run_id`, so runs never leak
   into each other. Re-indexing a run is idempotent: deterministic point IDs are upserted, then that run's stale
   points are deleted.
4. **Retrieval** (`rag/retrieve.py`).
   * Dense search uses Qdrant `query_points`.
   * BM25 (stemmed) runs over the same run's chunks.
   * The two rankings are fused with reciprocal-rank fusion.
   * **Competitor-aware.** If the question names a competitor, competitor-filtered lists join the fusion and
     chunks about other competitors are dropped (unless the question compares several).
   * **Intent-aware.** Pricing questions boost pricing facts and tiers, "complain" boosts customer sentiment, and
     "how do we win" boosts battlecards and recommendations.
   * Higher-confidence facts rank higher.
   * When an analysis chunk is retrieved, the facts it is built on are pulled in with it, so they can be cited
     directly.
5. **Answer** (`rag/answer.py`).
   * If there is chat history, a cheap call (`rag_condenser` role) first rewrites the follow-up as a standalone
     question.
   * Then a single `complete_json` call (`rag_answerer` role) returns `{covered, answer, citations, confidence,
     followups}`. The answer is at most ~180 words and gives numbers with their dates.

## Safety and honesty

* **Retrieved text is untrusted web data.** Every block is wrapped in `<<SOURCE n #nonce>> ... <<END n #nonce>>`. The
  nonce is random per call and fence-like text inside the data is defanged. The system prompt carries the standard
  spotlighting rules.
* **The answering model is tool-less.** It can only return JSON.
* **The user's question and chat history are sanitised** with `security.clean_snippet` (hidden characters and
  instruction-like sentences are removed). The history is fenced as data too.
* **Output validation.**
  * Every citation must refer to a block that was actually shown; unknown numbers or fact IDs are dropped.
  * Markers are renumbered `[1]..[n]` in reading order. If the model forgot inline markers, they are attached to the
    matching sentence.
  * URLs, links and HTML are stripped from the answer text, because sources are returned separately.
  * An answer that still looks like an injection is replaced.
  * Confidence is capped by the evidence: low-confidence facts can never produce a "high" answer.
* **Honest when uncovered.** If nothing is retrieved, or the model reports that the sources don't answer the question,
  the reply is *"The research doesn't cover that."* (`grounded: false`), with suggestions it *can* answer. An answer
  that claims coverage but has no valid citation is withheld rather than guessed.
* **Dates.** Prices, funding, headcount and news cite their `as_of` date. If the model omits it, the date is added.

## Setup

```bash
uv pip install -e '.[rag]'        # or: pip install 'resurge[rag]'   (qdrant-client >= 1.19, fastembed >= 0.9)
resurge index                     # index every finished run (or: resurge index <run_id>)
resurge chat                      # latest finished run; or: resurge chat <run_id>; /exit to quit
```

Indexing is lazy anyway: the first question about a run indexes it automatically.

**Local mode (default, private).** With no extra configuration, Qdrant runs embedded in the Resurge process and stores
data in `data/qdrant`. The store is locked only for the duration of each operation, so `resurge ui` and a CLI command
can share it. Local mode is meant for up to roughly 20,000 chunks (about 100 runs).

**Qdrant Cloud (free 1 GB cluster).** Create a free cluster at <https://cloud.qdrant.io>, then add its URL and API key
to your **`.env`** (never to `.env.example`, which is a public template):

```
QDRANT_URL=https://<your-cluster>.cloud.qdrant.io
QDRANT_API_KEY=<your key>
```

Resurge then uses the cloud cluster automatically and creates keyword payload indexes on `run_id` (as the tenant
key), `competitor` and `kind`. Runs indexed locally are re-indexed into the cloud lazily on first question.

| Variable | Default | Meaning |
|---|---|---|
| `QDRANT_URL`, `QDRANT_API_KEY` | unset | Use Qdrant Cloud instead of the local store |
| `RESURGE_QDRANT_MODE` | `auto` | `local` ignores `QDRANT_URL`; `cloud` requires it. `--local` / `RESURGE_LOCAL_ONLY=1` always uses the local store |
| `RESURGE_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | Any model from `fastembed.TextEmbedding.list_supported_models()` (a new model gets its own collection) |
| `FASTEMBED_CACHE_PATH` | `data/fastembed` | Where the embedding model is cached |

Keys are read from the environment only. They are never logged and never included in error messages.

**Privacy.** In local mode nothing leaves your machine except the LLM call that writes the answer. That call goes to
the same free-tier providers Resurge already uses, and `--private` / `RESURGE_LOCAL_ONLY` apply as usual. If Qdrant
Cloud is configured, the chunks and their embeddings are stored on your cluster. Chunks are public-web research text:
fact claims, quotes, URLs and the analysis built from them. Your questions are never stored there.

## Interfaces

* **CLI:** `resurge index [RUN_ID]`, `resurge chat [RUN_ID]`
* **Web dashboard** (`resurge ui`):
  * `GET /api/runs/{run_id}/chat/status` returns `{indexed, chunks, backend}`.
  * `POST /api/runs/{run_id}/chat` takes `{"question": str (up to 500 chars), "history": [{"role": "user"|"assistant",
    "content": str}] (up to 8 turns)}`.
  * Errors: `422` for bad input, `404` for an unknown run, `409` for a run without a report, `503` when RAG or the
    models are unavailable, and `403` for cross-origin POSTs.
* **MCP:** `ask_research(question, run_id=None)` (defaults to the latest finished run).
* **Python:**

```python
from resurge import rag
rag.available()                                   # optional deps installed?
rag.index_run(settings, report, storage=None)     # {"chunks", "backend", ...}; raises rag.RagUnavailable
await rag.ask(settings, run_id, question, history=None, llm=None)
# {"answer", "citations": [{"n", "fact_id", "claim", "quote", "url", "title", "competitor"}],
#  "confidence": "high"|"medium"|"low", "followups", "grounded", "backend"}
rag.status(settings, run_id)                      # {"indexed", "chunks", "backend": "qdrant-cloud"|"qdrant-local"|"unavailable"}
```

Model routing for the two roles lives in `config/models.yaml`:

* `rag_answerer`: gpt-oss-120b on Groq, then Gemini Flash-Lite, and so on
* `rag_condenser`: Gemini 3.1 Flash-Lite, then Gemma-4-26B, then gpt-oss-20b on Groq (non-reasoning models first, so the 200-token budget isn't spent on thinking)

Answers typically take 1 to 2 seconds.
