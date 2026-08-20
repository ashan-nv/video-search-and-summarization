# VSS ⇄ BYO Agent Adapter (POC)

Reference adapter letting the VSS UI drive a **NemoClaw / OpenClaw** agent, so a user can
bring their own agent or harness instead of the built-in `vss-agent`.

```
VSS UI ──HTTP/SSE──> adapter ──WebSocket──> OpenClaw gateway ──> skills ──> VSS backends
```

Design rationale, the full decision log, and the (undocumented) OpenClaw gateway protocol
notes are in **[DECISIONS.md](DECISIONS.md)** — read that before changing anything here.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/chat/stream` | OpenAI-shaped `{messages}` in, `text/event-stream` out |
| `GET` | `/health` | liveness + resolved gateway/session config |
| `GET` | `/v1/skills` | manifest: name, path, `requirements` per skill |
| `GET` | `/v1/skills/<name>` | one skill's `SKILL.md` (fetched on demand by the agent) |
| `GET` | `/v1/skills/<name>/bundle.tar.gz` | one skill incl. `scripts/` and `references/` |
| `GET` | `/v1/skills/bundle.tar.gz` | all skills, version-matched to this deployment |
| `GET` | `/v1/skills/env` | resolved VSS base URLs (replaces `HOST_IP` guessing) |

The request/response shape is not invented — it is what the VSS UI already speaks
(`packages/nemo-agent-toolkit-ui/pages/api/chat.ts`). Any backend implementing
`/chat/stream` can be swapped in by changing one env var, which is the point.

## Run

```bash
ADAPTER_PORT=9098 \
OPENCLAW_GATEWAY_TOKEN="$(nemoclaw <sandbox> gateway-token | head -1)" \
  python3 adapter.py
```

| Env | Default | Notes |
|---|---|---|
| `OPENCLAW_GATEWAY_URL` | `ws://localhost:18789/` | OpenShell forward to the sandbox gateway |
| `OPENCLAW_GATEWAY_TOKEN` | *(required)* | `nemoclaw <sandbox> gateway-token` |
| `OPENCLAW_SESSION_PREFIX` | `agent:main:vss` | one session per UI conversation |
| `ADAPTER_PORT` | `9099` | |
| `ADAPTER_TURN_TIMEOUT` | `600` | seconds |
| `VSS_SKILLS_DIR` | `~/video-search-and-summarization/skills` | source for the skills endpoints |
| `VSS_HOST_ALIAS` | `host.openshell.internal` | advertised by `/v1/skills/env` |
| `ADAPTER_BOOTSTRAP` | `1` | set `0` to disable first-turn context injection |
| `ADAPTER_PUBLIC_URL` | `http://<alias>:<port>` | base URL the agent is told to fetch from |

## Bootstrap

On a session's **first turn** the adapter prepends a deployment-context block: where VSS
is, the skills index (name + description), how to fetch a skill's full instructions, and
the VSS conventions. ~6.6 KB.

This is what makes BYO work without an install step — it needs nothing from the harness
but the ability to accept text, and skill bodies (~345 KB across 18 skills) stay remote
until one is actually needed.

Requires `websocket-client` (`pip install websocket-client`).

## Point the UI at it

The UI reads `NEXT_PUBLIC_*` at **container start** (`next-runtime-env`, `/__ENV.js`), so
this is an env change plus a restart — no rebuild:

```yaml
NEXT_PUBLIC_SIDEBAR_CHAT_HTTP_CHAT_COMPLETION_URL: http://<docker-bridge-gw>:9098/chat/stream
```

`chat.ts` is a Next.js *edge API route*, so the fetch happens server-side from inside the
UI container — the adapter does not need to be publicly reachable.

> **Gotcha:** if the chat is in WebSocket mode the adapter is bypassed entirely and
> silently, because the WS URL still points at the old backend. `sessionStorage`
> overrides the env default. See DECISIONS.md known issue #6.

## Verify which backend served a turn

```bash
grep 'POST /chat/stream' adapter.log          # adapter received it
docker logs vss-agent | grep WebSocket        # old backend received it
```

On the gateway, only this adapter creates `agent:main:vss-<conversation-id>` sessions, so
their presence in `sessions.list` is unambiguous proof.

## Status

POC. Known gaps — no auth on the adapter, `BOOTSTRAP.md` re-runs per conversation, and
the model's chain-of-thought can leak into replies. All tracked in DECISIONS.md §6.
