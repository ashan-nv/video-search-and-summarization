# VSS ⇄ BYO Agent — Decisions & Context

**Status:** POC wired and working end to end.
**Date:** 2026-08-19
**Reference environment:** Brev/OCI instance, 4x L40S, VSS `search` profile.

---

## 1. Goal

Let someone **bring their own agent or harness** (OpenClaw, Hermes, …) and have it
drive VSS from the VSS UI:

```
VSS UI ──HTTP/SSE──> agent backend ──skills──> VSS backends ──> results rendered in VSS UI
```

The agent uses VSS **skills** to do the work (create an alert rule, search video),
and results come back to the UI to be rendered by **existing VSS components**.

---

## 2. Decisions

### 2.1 Transport: HTTP + SSE (not WebSocket)

**Decided:** UI talks to the agent backend over HTTP with an SSE response stream.

Why:
- The VSS UI already parses SSE (`data:` frames, `[DONE]` sentinel).
- Agent turns are long (deploy polls run minutes); buffering to one JSON blob is unusable.
- SSE is unidirectional, which fits a request/response turn. No upgrade negotiation,
  works over HTTP/2, reconnects natively.
- WebSocket upgrades through proxies are fragile — verified during this work: a WS
  upgrade test through cloudflared returned 200 over HTTP/2 and only 101 over HTTP/1.1.

**Deferred:** approvals need a client→server channel mid-turn. Plan is SSE for the turn
plus a small `POST /v1/agent/turns/{id}/respond`, *not* a WebSocket upgrade.

### 2.2 Contract shape: superset of OpenAI streaming chat

**Decided:** the BYO contract is OpenAI-shaped chat + SSE.

This was not designed — it is **already what the VSS UI speaks**. `chat.ts` sends
`buildOpenAIChatPayload` (`{messages, model, temperature, …}`) and reads content from
`value | output | answer | choices[0].message.content | choices[0].delta.content`.

Consequence: a text-only BYO agent that speaks OpenAI works with **zero changes**.
Richer harnesses can add event types later.

### 2.3 Who writes the adapter: the agent side, not VSS

**Decided:** VSS publishes a small contract; the BYO integrator implements it.
VSS ships **one reference adapter** (OpenClaw) that others copy.

Rejected: VSS maintaining a driver per harness. That is not BYO, it is
"VSS supports a list of agents," and it does not scale.

### 2.4 The agent does NOT render results

**Decided:** the agent emits data/references; **existing VSS components render**.

`SearchComponent` already has an `agentSearchResults: SearchData[]` slot and a
`registerChatAnswerHandler` subscription. `AlertsComponent` is the same story. The
agent should never produce markdown tables of video clips.

### 2.5 Result payloads must not pass through the model's token stream

**Decided (target design):** the model emits prose + a compact reference; bulk data
travels around it.

**Current VSS behaviour is the anti-pattern.**
`search/lib-src/utils/agentResponseParser.ts` (`extractSearchResultsFromAgentResponse`)
parses the **entire Search API JSON payload** out of the agent's chat text — brace
matching and ```json fence detection. That means the LLM transcribes a 47-result payload
token by token: expensive, slow, and prone to truncation and hallucinated fields.

**POC shortcut (accepted):** keep the parse-from-text pathway but shrink what is parsed
from the whole result set to a reference:

```json
{"vss_artifact": {"kind": "search_results", "query_id": "q-123"}}
```

UI resolves `q-123` against the search API it already calls. ~20 tokens instead of ~5000.

Chosen for the POC specifically because it **requires nothing of the harness** — any
agent that emits text can do it. Typed SSE artifact events would require every BYO
harness to cooperate, i.e. per-harness drivers again.

**Target design (post-POC):** real SSE named events so the browser dispatches natively
(`es.addEventListener('artifact', …)`) instead of regex-scraping text:

| Event | Purpose |
|---|---|
| `token` | assistant text delta |
| `step` | tool/skill start, progress, end |
| `artifact` | structured result (clip set, incident, rule) |
| `approval_request` | blocks pending a `respond` call |
| `error` / `done` | terminal |

Payload style by size: **inline** for small/final (a created rule, a status);
**handle/reference** for large or paginated (search results, incident lists).

Anchor every artifact to a `step` id — artifacts arrive out of band relative to tokens,
so arrival order will not tell the UI where to place them.

### 2.6 UI: keep the NAT UI for the POC, replace it later

**Context:** the team wants to drop both `vss-agent`/NAT core *and* the NAT UI
(`packages/nemo-agent-toolkit-ui`, 133 files — all chat UI: Chat, Chatbar, Markdown,
Settings, Sidebar). `packages/nv-metropolis-bp-vss-ui` (116 files) owns the feature
areas: `alerts`, `search`, `video-management`, `dashboard`, `map`.

**Decided:** use the existing NAT chat UI for the POC anyway. The POC's risk is the
*contract*, which is UI-independent. Rewriting the chat shell would take longer than the
entire POC and would teach nothing about whether BYO works.

**Rule while here:** do not *extend* the NAT package. New code goes in
`nv-metropolis-bp-vss-ui` or the adapter, so the eventual removal stays a deletion.

**Use the sidebar, not the chat tab** — the sidebar is what is wired into the feature
tabs via `registerChatAnswerHandler` / `registerSidebarChatEventSubscriber`.

### 2.7 Realtime alerts do NOT go through the agent

**Decided:** alerts stream directly from alert-bridge to the UI, as today.

Routing a push feed through an LLM turn adds seconds of latency and per-token cost to
something that is just a feed. Use the agent for *actions on* alerts ("create a rule for
this", "explain this incident"), not for viewing them.

### 2.8 Skills distribution: serve from VSS

**Decided — implemented in the adapter for the POC** (belongs in VSS proper later):

```
GET /v1/skills/bundle.tar.gz   # version-matched to this deployment
GET /v1/skills/env             # resolved base URLs
```

`/v1/skills/env` matters more than it looks: skills need to know *where* VSS is, and
that is genuinely complicated (`HOST_IP` in-sandbox, `VSS_PUBLIC_URL` on k8s, Compose
discovery fallback, per-profile path prefixes `/vst`, `/alert-bridge`, `/va-mcp`). Today
that is prose in `deployment_resolution.md` + an export in `ENV.md`. Serving it stops
every BYO agent from re-deriving host resolution and getting it subtly wrong.

### 2.8b Bootstrap by prompt, not by install

**Decided:** the adapter prepends a deployment-context block to a session's **first
turn**, carrying: where VSS is, the skills index (name + description), how to fetch a
skill's full instructions, and the VSS conventions.

Why this and not `skill install` + uploaded workspace docs:

- It requires **nothing of the harness** except accepting text. `skill install` assumes an
  OpenClaw-shaped workspace; a hosted BYO agent has no workspace at all.
- It is always current — no snapshot to drift, no `make skills-sync`.
- The index is inlined (~6.6 KB) rather than fetched, because a round trip is a step a
  harness might simply not make. Skill **bodies** stay remote and are fetched on demand
  (`GET /v1/skills/<name>`), so the ~345 KB of instructions never enters context.

Prepended to the first user turn rather than sent as its own message: a standalone
bootstrap turn makes the agent reply to it, surfacing a stray message to the user.

Verified: on a fresh session, asked which skill searches archived video, the agent
answered `vss-search-archive` — information it could only have from the injected index.

### 2.8c Skill requirements are real, and most VSS skills are host-oriented

Audit of the 18 skills, by what their `SKILL.md` actually reaches for:

| Requirement | Skills |
|---|---|
| `docker` | 17 |
| `curl` | 16 |
| `jq` | 11 |
| `mcp` | 5 |
| `uv` | 5 |
| `vss-repo` (source checkout + CLI) | 3 |

The nemoclaw sandbox provides `curl`, `jq`, `python3`, `git`, `node` — but **not
`docker` and not `uv`**. So `vss-search-archive`, `vss-summarize-video`, and
`vss-build-vision-agent` cannot run there at all: they drive a host CLI
(`vss search run` via `uv`) against a repo checkout, not an HTTP API.

Two consequences:

1. The manifest's `requirements` must be **detected per skill**, not hardcoded. The
   original uniform `[shell, curl, network]` was actively misleading.
2. Requirements belong **inline in the bootstrap index**, next to each skill name —
   not in a footnote. Observed failure: with requirements absent from the index, asked
   to search archived video, the agent refused for a plausible-but-wrong reason ("the
   orchestrator MCP is unreachable") because that was the nearest blocker it knew about.
   That would send a user chasing the wrong fix. With `[needs: uv, vss-repo, docker, curl,
   jq]` inline, it correctly reported `uv`, `vss-repo`, and `docker` missing while noting
   `curl` and `jq` present.

Detection is text-based, so it is a hint rather than a contract — some mentions are
fallbacks or prohibitions ("do not run docker directly"). Still far better than a uniform
claim. A future `requirements:` block in SKILL.md frontmatter would make it exact.

### 2.8d Archive search over HTTP

**Decided:** the adapter exposes `POST /v1/search`, which runs the same host CLI
(`vss search run <mode> --raw`) the skill would and returns its `SearchOutput`.

`vss-search-archive` needs `uv` plus a VSS source checkout (2.8c). A sandboxed agent has
neither, and a hosted BYO agent never will — so the skill was simply unrunnable. Rather
than installing a toolchain and a source tree into every agent environment, run it once
on the host and expose HTTP, which is the one thing every agent can reach.

Body: `{mode: embed|attribute|fusion|object, query, top_k, source_type, video_sources}`.
Arguments are built as a list and passed without a shell, and every value is validated or
coerced, so a request body cannot inject flags or commands.

**The egress policy must list the adapter's port.** Verified the hard way: with 9098
absent from `vss_nemoclaw_policy.yaml`, the agent's call was denied before leaving the
sandbox and it reported the deployment unreachable — a plausible but wrong diagnosis.
Adding port 9098 and re-running `policy-add` (version 5) fixed it.

Verified end to end: UI -> adapter -> gateway -> agent -> HTTP -> adapter -> uv -> CLI ->
Elasticsearch. The agent's request reaches `/v1/search` and it reports the true blocker
(`Search index 'mdx-embed-filtered-*' does not exist. Please ensure videos have been
ingested`). **Not yet verified against real results** — nothing has been ingested on this
deployment, so the happy path with actual hits is untested.

### 2.8e Ingestion works; retrieval returns nothing (open VSS issue)

To test `/v1/search` against real data, a clip was ingested through the documented
Agent-backed flow (`POST /api/v1/videos` -> upload -> `/complete`). It succeeded:
`"embeddings generated", chunks_processed: 2`, and `mdx-embed-filtered-2025-01-01` now
holds 4 docs with real 768-dim vectors at `llm.visionEmbeddings[0].vector`, mapped as
`dense_vector`. Index model (`cosmos-embed1-448p-anomaly-detection`) matches the model
rt-embed serves.

**But every search returns `{"data": [], "search_messages": []}`** — zero hits, no
diagnostic — including with `--min-cosine-similarity 0.0`, an explicit `--video-source`,
explicit `--source-type`, and explicit time bounds covering the indexed window. The CLI
has the right index recorded in `~/.vss/config.json`.

Unresolved: where the *query-side* text embedding comes from. `rt-embed`'s OpenAPI
exposes only files/metrics/health paths — no text-embedding route — and `/v1/embeddings`
and `/v1/embed` both 404. Indexing is a video-embedding path; the query path is not
obviously served by the same component.

This is a VSS retrieval issue, not an adapter one: `/v1/search` faithfully returns
whatever the CLI produces. Worth noting that the empty `search_messages` makes this
silent — a caller cannot distinguish "no matches" from "retrieval is misconfigured".

**Also found and fixed:** `vss-agent` was handing out upload URLs built from a stale
`VSS_AGENT_EXTERNAL_URL` pointing at a dead cloudflare hostname, so the documented
ingestion flow failed at step 2. Root cause was **not** a bad config value —
`resolved.yml` already held the current hostname; the *running container* predated the
regeneration and was still carrying the old env. Recreating `vss-agent` picked up the
current config. Worth remembering that `resolved.yml` being correct says nothing about
what a long-running container actually has.

Consequence to know about: the corrected URL points at the public tunnel origin, which
is behind HTTP basic auth. Browsers authenticate once and cache; **programmatic callers
(agents, skills, scripts) must either send credentials or use the internal origin**
(`http://localhost:7777`). Exempting the upload path from auth would let anyone upload
video to the deployment, so the auth stays and callers adapt.

### 2.9 BYO scope: "their harness, our sandbox" — not "their sandbox"

**Decided:** support Case A. Defer Case B.

- **Case A — their harness, our sandbox.** `nemoclaw onboard --agent hermes`. Everything
  built here applies: egress policy, `skill install`, workspace docs, `HOST_IP`. Cheap.
- **Case B — their own sandbox.** Requires real authn/authz at an ingress in front of
  the VSS backends; today isolation comes from the sandbox egress policy, which only
  holds while the agent runs inside a sandbox we control. Also
  `host.openshell.internal` does not exist outside an OpenShell sandbox, so endpoint
  resolution has to be served rather than assumed. Case B is a real project, not a
  distribution tweak.

### 2.10 Multi-user is a correctness problem, decide before building further

One sandbox = one agent = one workspace = one `MEMORY.md`. `AGENTS.md` explicitly
forbids loading `MEMORY.md` in shared contexts. Front that with a shared VSS UI and every
user reads/writes one agent's memory. Options: sandbox-per-user, or `openclaw agents`
isolated agents keyed by VSS user. Retrofitting after launch is painful.

---

## 3. The OpenClaw gateway protocol (learned by probing — undocumented)

`openclaw gateway --help`: *"Run, inspect, and query the **WebSocket** Gateway."*
There is no OpenAI-compatible REST surface — `/v1/models` and `/v1/chat/completions`
return **200 only because the Control UI SPA catch-all serves HTML**.

`openclaw agent` (CLI) is **not** a viable transport: it cannot reach the gateway from an
exec context, and its embedded fallback cannot resolve `inference.local` because
inference is brokered *through* the gateway.

Handshake and quirks, all verified:

- Server opens with `connect.challenge` + nonce. **The nonce is not echoed back.**
- **Omit `device` entirely** to skip device-identity/pairing. Sending a partial `device`
  demands `publicKey` and pulls you into the pairing flow. This single fact collapsed the
  adapter from "implement crypto pairing" to "send a token."
- `scopes` must be named explicitly: `["operator.read","operator.write"]`. `"*"` is rejected.
- `client.id` must be `openclaw-control-ui`.
- **`connect` must complete before any other request** — otherwise
  `"invalid handshake: first request must be connect"`.
- `sessions.messages.subscribe` → `{key}` (not `sessionKey`)
- `chat.send` → `{sessionKey, message, idempotencyKey}`
- `sessions.create` → `{key}` only (no `kind`)
- Streaming: `chat` events with `state:"delta"` + `deltaText`, terminated by `state:"final"`.
- Also available: `caps:["tool-events"]`, `session.tool`, `exec.approval.requested`,
  `sessions.abort`, `tools.catalog`, `tools.invoke`.

The gateway's JSON-schema errors are precise — use it as an oracle when extending.

---

## 4. What is built and running

### Adapter — `/home/nvidia/vss-agent-adapter/adapter.py`

~190 lines, stdlib + `websocket-client`. Listens on `0.0.0.0:9098`.

```
POST /chat/stream   OpenAI-shaped {messages} -> text/event-stream
GET  /health
```

Maps gateway `chat` delta events → `data: {"choices":[{"delta":{"content": …}}]}`, and
`agent` tool events → `intermediate_data:` lines (which the NAT UI renders as
`<intermediatestep>` progress markers).

Sessions: one per conversation, keyed off the `Conversation-Id` header the VSS UI already
sends, as `agent:main:vss-<conv>`.

Run it:

```bash
cd /home/nvidia/vss-agent-adapter
ADAPTER_PORT=9098 \
OPENCLAW_GATEWAY_TOKEN="$(nemoclaw demo gateway-token | head -1)" \
  python3 adapter.py
```

### UI wiring

`deploy/docker/resolved.yml` line ~1459 (backup: `resolved.yml.bak-*`):

```yaml
NEXT_PUBLIC_SIDEBAR_CHAT_HTTP_CHAT_COMPLETION_URL: http://172.19.0.1:9098/chat/stream
```

`172.19.0.1` is the `vss_default` bridge gateway. **No tunnel is needed**: `chat.ts` is a
Next.js *edge API route*, so the `fetch` happens server-side from inside the UI container,
not from the browser.

The UI uses `next-runtime-env` (`/__ENV.js`), so `NEXT_PUBLIC_*` are injected at
**container start, not build time** — changing the backend is an env change plus restart,
no rebuild:

```bash
cd /home/nvidia/video-search-and-summarization/deploy/docker
docker compose -p vss -f resolved.yml \
  --env-file developer-profiles/dev-profile-search/.env \
  --env-file developer-profiles/dev-profile-search/generated.env \
  up -d --no-deps --force-recreate vss-ui
```

`NEXT_PUBLIC_WEB_SOCKET_DEFAULT_ON=false` (and the sidebar variant), so HTTP mode is the
default and the SSE path is used.

> `resolved.yml` is **generated** by the deploy tooling — this edit will be overwritten on
> the next profile regeneration. Fine for a POC; make it a real env var before it matters.

### Verified

- Adapter → gateway → agent, streamed: 15 SSE frames.
- UI `/api/chat` → adapter (seen in adapter log from `172.19.0.6`) → clean answer.

---

## 5. NemoClaw deployment (built during this work)

| Setting | Value |
|---|---|
| Sandbox | `demo` |
| Harness | OpenClaw 2026.6.10 |
| Model | `nvidia/nemotron-3-super-120b-a12b` (build.nvidia.com) |
| Install ref | `v0.0.80` |
| Dashboard | `https://<your-tunnel-or-host>:18789/#token=<gateway-token>` |
| Policy | v4, `vss` preset over installer's balanced tier |
| Skills | 18 installed |
| Hooks | enabled; token in `~/.nemoclaw_hooks_token` |
| API key | `~/.nvidia_api_key` (0600) |

Get the gateway token: `nemoclaw demo gateway-token`.
The token goes in the URL **fragment** (`#token=`), not a paste field — fragments are
never sent to the server, so the token does not traverse Cloudflare's edge.

**The tunnel is load-bearing.** `CHAT_UI_URL` is baked at onboard and `gateway.*` is
read-only afterward. The cloudflared quick tunnel (PID varies, `--url http://localhost:18789`)
has **no supervisor** — if it dies the hostname changes and the sandbox must be re-onboarded.
Add a systemd unit before relying on this.

---

## 6. Known issues / deferred

1. **Session choice matters.** Reusing `agent:main:main` made the agent reply as if
   answering a heartbeat poll (walking its `AGENTS.md` checklist). Fresh per-conversation
   sessions fix that — but each new session re-ran `BOOTSTRAP.md`, so every conversation
   opened with an environment probe instead of an answer.

   **Resolved** by retiring `BOOTSTRAP.md` in the sandbox workspace (renamed
   `.retired`; the file itself says "Read this once, then delete it" and the agent never
   did). The adapter's first-turn context (2.8b) supplies the same deployment information
   on every session, which is strictly better: it cannot go stale and needs no workspace.
2. **Nemotron sometimes leaks chain-of-thought into message content**, despite the
   gateway reporting `thinkingDefault: "off"`. **This is not filterable** — verified that
   the final message carries a single `type: "text"` content part with no separate
   reasoning field, so reasoning is indistinguishable from the answer. It is also
   intermittent: ordinary turns stream clean prose; it surfaced on bootstrap-heavy turns.
   Mitigation is a different model (`openai/gpt-oss-120b`) or prompt work, **not** a regex
   in the adapter, which would eat legitimate content.
   Harness *sentinels* (`NO_REPLY`, `HEARTBEAT_OK`) are a separate matter — those are
   exact tokens and are now stripped by `SentinelFilter`, which holds back a short tail so
   a sentinel split across two deltas is still caught without breaking streaming.
3. ~~**Adapter has no auth.**~~ **Fixed.** Callers must come from an allowlisted CIDR
   (`127.0.0.1`, `::1`, `172.16.0.0/12` by default — loopback plus docker bridges), and an
   optional `ADAPTER_TOKEN` can be required on top. The token is accepted via
   `Authorization: Bearer`, `X-Adapter-Token`, **or `?token=`** — the query form exists
   because `chat.ts` sends a fixed header set with no auth header but passes its
   configured URL through verbatim, so a query param is the only way to authenticate
   without UI changes. Default deployment is allowlist-only: requiring a token would mean
   embedding it in `ADAPTER_PUBLIC_URL`, which lands in the agent's prompt where it could
   be echoed back to a user.
4. **Artifact ingest, when built, must not be keyed on session id alone** — that is
   forgeable; anyone reaching the bridge could inject results into a live turn. Mint a
   short-lived per-turn token.
5. ~~**SSE keepalives**~~ **Done.** A `: keepalive` comment every `ADAPTER_SSE_KEEPALIVE`
   seconds (default 15) whenever the gateway is silent, so cloudflared/haproxy do not drop
   a stream while a skill runs.

   Fixing this surfaced a worse bug: the SSE response was sent on a kept-alive HTTP/1.1
   connection with no `Content-Length`, so **the client could not tell the response had
   ended**. A turn that finished in seconds left curl blocked until its 240s timeout, and
   would leave the UI stuck showing "streaming" indefinitely. Now sends
   `Connection: close` and sets `close_connection`; the same turn completes in 20s.
6. **WebSocket mode silently bypasses the adapter.** `Chat.tsx` reads
   `sessionStorage.getItem('webSocketMode')` and that **overrides**
   `NEXT_PUBLIC_WEB_SOCKET_DEFAULT_ON=false`. With WS mode on, the UI talks to
   `NEXT_PUBLIC_*_WEBSOCKET_CHAT_COMPLETION_URL` (still the old `vss-agent`) and the
   adapter never sees a request — with no visible error. Symptom: "the agent works" but
   the adapter log is empty and no `agent:main:vss-*` session appears on the gateway.
   Fix: toggle WebSocket Mode off in chat Settings, or
   `sessionStorage.removeItem('webSocketMode')`. The adapter speaks HTTP/SSE only.
   To verify which backend served a turn: `grep 'POST /chat/stream' adapter.log`,
   gateway `sessions.list`, and `docker logs vss-agent | grep WebSocket`.
7. **Skills are copied into the sandbox, not mounted.** Editing the repo does nothing
   until `nemoclaw <sb> skill install` is re-run, and nothing warns you it is stale. Write
   a `make skills-sync`. Stamp a git SHA into `SKILL.md` frontmatter so drift is detectable.

---

## 7. Bugs found in VSS docs/tooling (worth upstream PRs)

1. **`deploy_nemoclaw.ipynb` cell 10 pins a model that does not exist.**
   `qwen/qwen3.5-122b-a10b` is not in the build.nvidia.com catalog (no Qwen models are).
   Running the notebook as-is onboards a sandbox that fails on first inference. Blank it so
   the installer default applies.
2. **`nemoclaw/README.md` step 7 bricks the gateway.** It sets `hooks.enabled=true`
   without `hooks.token`; the gateway then refuses to start and crash-loops with
   `hooks.enabled requires hooks.token`. The notebook knows (`AGENT_HOOKS_TOKEN` in
   preflight); the shell flow does not.
3. **The skill install loop misses a skill.** `for skill in "$REPO"/skills/*/` descends one
   level, silently skipping `skills/benchmarking/benchmark-video-summarization/` despite
   its valid `SKILL.md`. Do not reproduce this glob in tooling.
4. **`deploy_nemoclaw.ipynb` cell 10's comment lies about its own code** — it claims to
   clear the custom-endpoint vars so the installer picks `NEMOCLAW_PROVIDER=build`, but it
   only assigns `NVIDIA_API_KEY` and `NEMOCLAW_MODEL`. Since cell 12 derives
   `NEMOCLAW_PROVIDER = "custom" if NEMOCLAW_ENDPOINT_URL else "build"`, running cell 6 or 8
   first in the same kernel silently onboards against a stale endpoint.

---

## 8. Next steps

1. Delete `agentResponseParser.ts`'s full-payload parsing; shrink to the `vss_artifact`
   reference marker. Update `vss-search-archive` to emit it.
2. ~~Build `GET /v1/skills/bundle.tar.gz` + `GET /v1/skills/env`~~ — **done** (in the
   adapter; move into VSS proper when the contract firms up). Also serves `GET /v1/skills`
   as a manifest with a per-skill `requirements` list, so a harness lacking a shell can
   tell up front that it cannot run them.
3. Decide multi-user isolation (§2.10) before more UI work.
4. Write the contract spec as a reviewable doc so a BYO integrator has something to build
   against.
5. Post-POC: typed SSE events (§2.5), `registerArtifactHandler(kind, …)` replacing
   `registerChatAnswerHandler`, `Last-Event-ID` resumability for multi-minute turns.
