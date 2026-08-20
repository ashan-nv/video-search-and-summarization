#!/usr/bin/env python3
"""VSS <-> OpenClaw agent adapter (POC).

Exposes an OpenAI-shaped streaming chat endpoint that the VSS UI can point at
via NEXT_PUBLIC_SIDEBAR_CHAT_HTTP_CHAT_COMPLETION_URL, and drives an OpenClaw
gateway over its WebSocket protocol (v4).

  POST /chat/stream   {"messages":[...]}  -> text/event-stream
  GET  /health

Gateway protocol notes (discovered empirically against OpenClaw 2026.6.10):
  - server opens with event connect.challenge (nonce is NOT echoed back)
  - connect params: omit `device` entirely to skip device-identity/pairing
  - scopes must be named explicitly; "*" is rejected
  - sessions.messages.subscribe takes {"key": ...}, chat.send takes
    {"sessionKey", "message", "idempotencyKey"}
"""
import io, json, os, queue, re, shutil, subprocess, tarfile, threading, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import websocket

GATEWAY_URL = os.environ.get("OPENCLAW_GATEWAY_URL", "ws://localhost:18789/")
GATEWAY_TOKEN = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")
SESSION_PREFIX = os.environ.get("OPENCLAW_SESSION_PREFIX", "agent:main:vss")
LISTEN_PORT = int(os.environ.get("ADAPTER_PORT", "9099"))
TURN_TIMEOUT = int(os.environ.get("ADAPTER_TURN_TIMEOUT", "600"))
SKILLS_DIR = os.environ.get(
    "VSS_SKILLS_DIR",
    os.path.expanduser("~/video-search-and-summarization/skills"))
HOST_ALIAS = os.environ.get("VSS_HOST_ALIAS", "host.openshell.internal")

# Harness sentinels that must never reach a user. OpenClaw emits these for
# heartbeat turns; they are exact tokens, unlike the model's chain-of-thought
# which arrives as ordinary prose and is NOT safely strippable.
BOOTSTRAP_ENABLED = os.environ.get("ADAPTER_BOOTSTRAP", "1") != "0"
ADAPTER_PUBLIC_URL = os.environ.get(
    "ADAPTER_PUBLIC_URL", f"http://{HOST_ALIAS}:{os.environ.get('ADAPTER_PORT', '9099')}")

# Archive search over HTTP.
#
# `vss-search-archive` drives a host CLI (`vss search run` via uv) against a
# source checkout. A sandboxed or hosted agent has neither, so the skill is
# unrunnable there -- see DECISIONS.md 2.8c. This endpoint runs the same CLI on
# the host and exposes it as plain HTTP, which is the one thing every agent can
# reach.
VSS_REPO_ROOT = os.environ.get(
    "VSS_REPO_ROOT", os.path.expanduser("~/video-search-and-summarization"))
UV_BIN = os.environ.get("UV_BIN") or shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")
SEARCH_MODES = ("embed", "attribute", "fusion", "object")
SEARCH_TIMEOUT = int(os.environ.get("ADAPTER_SEARCH_TIMEOUT", "180"))

SENTINELS = ("NO_REPLY", "HEARTBEAT_OK")
_HOLD = max(len(x) for x in SENTINELS) - 1


# What a skill's SKILL.md has to mention for us to claim it needs that tool.
# Audited against the 18 VSS skills: 17 reference docker, 5 need uv, 3 need a
# repo checkout. A sandboxed agent typically has curl/jq/python3 but NOT docker
# or uv, so advertising a uniform requirement set is actively misleading.
_REQUIREMENT_PATTERNS = [
    ("uv", re.compile(r"(?:^|[^a-z])uv (?:run|tool)|uvx ", re.M)),
    ("vss-repo", re.compile(r"VSS_REPO_ROOT|services/agent")),
    ("docker", re.compile(r"(?:^|[^a-z-])docker ")),
    ("curl", re.compile(r"\bcurl\b")),
    ("jq", re.compile(r"\bjq\b")),
    ("mcp", re.compile(r"mcp", re.I)),
]


def _detect_requirements(skill_md):
    """Best-effort: what a skill's instructions actually reach for.

    Derived from the text, so it is a hint rather than a contract -- some
    mentions are fallbacks or prohibitions ("do not run docker directly").
    Still far better than claiming every skill needs the same three things.
    """
    try:
        with open(skill_md, encoding="utf-8", errors="replace") as fh:
            body = fh.read()
    except OSError:
        return []
    return [name for name, pat in _REQUIREMENT_PATTERNS if pat.search(body)]


def _frontmatter_description(skill_md):
    """Pull `description:` out of SKILL.md YAML frontmatter (agentskills.io)."""
    try:
        with open(skill_md, encoding="utf-8", errors="replace") as fh:
            if fh.readline().strip() != "---":
                return ""
            desc, cont = "", False
            for line in fh:
                if line.strip() == "---":
                    break
                if cont and (line.startswith("  ") or line.startswith("\t")):
                    desc += " " + line.strip()
                    continue
                cont = False
                if line.lower().startswith("description:"):
                    val = line.split(":", 1)[1].strip().strip("\"'")
                    # `>` / `|` are YAML block-scalar markers, not content --
                    # the text itself is on the following indented lines.
                    desc = "" if val[:1] in (">", "|") else val
                    cont = True
            return " ".join(desc.split())
    except OSError:
        return ""


def skills_manifest():
    """One entry per skill dir containing a SKILL.md.

    Walks two levels: skills/<name>/ and skills/<group>/<name>/ -- the canonical
    `skills/*/` glob misses the nested ones.
    """
    entries = []
    if not os.path.isdir(SKILLS_DIR):
        return entries
    for root, dirs, files in os.walk(SKILLS_DIR):
        if root.count(os.sep) - SKILLS_DIR.count(os.sep) > 2:
            dirs[:] = []
            continue
        if "SKILL.md" in files:
            entries.append({
                "name": os.path.basename(root),
                "path": os.path.relpath(root, SKILLS_DIR),
                "description": _frontmatter_description(
                    os.path.join(root, "SKILL.md")),
                "requirements": _detect_requirements(
                    os.path.join(root, "SKILL.md")),
            })
            dirs[:] = []
    return sorted(entries, key=lambda e: e["name"])


_BOOTSTRAP_CACHE = {}


def BOOTSTRAP_TEXT():
    if "text" not in _BOOTSTRAP_CACHE:
        _BOOTSTRAP_CACHE["text"] = build_bootstrap(
            skills_manifest(), ADAPTER_PUBLIC_URL)
    return _BOOTSTRAP_CACHE["text"]


def build_bootstrap(manifest, base_url):
    """Context prepended to a session's first turn.

    This is the whole BYO story in one string: it needs nothing from the harness
    but the ability to accept text, so it works for any agent -- unlike
    `skill install`, which assumes an OpenClaw-shaped workspace.

    The skills *index* is inlined (small, and removes a round trip that a harness
    might simply not make). Skill *bodies* stay remote and are fetched on demand.
    """
    lines = [
        "# VSS deployment context",
        "",
        "You are connected to a NVIDIA VSS (Video Search and Summarization)",
        "deployment. The user is talking to you from the VSS UI.",
        "",
        "## Reaching VSS",
        f"Base URL for VSS agent APIs: {base_url}",
        f"Resolved service endpoints: GET {base_url}/v1/skills/env",
        "Inside a sandbox, always call the host alias from that document -- never",
        "`localhost` and never a literal IP, or the egress policy denies the call.",
        "",
        "## Your skills",
        "Each skill below is a set of instructions for one task. When a request",
        "matches a description, FETCH that skill's instructions first and follow",
        f"them:  GET {base_url}/v1/skills/<name>",
        "Do not guess at a skill's steps from its description alone.",
        "",
    ]
    for e in manifest:
        # Requirements go next to the name, not in a footnote: the agent picks a
        # skill from this list, so it has to see the cost at selection time.
        # Without this it refuses for plausible-but-wrong reasons.
        needs = ", ".join(e.get("requirements") or []) or "none"
        lines.append(
            f"- {e['name']} [needs: {needs}]: "
            f"{e.get('description') or '(no description)'}")
    lines += [
        "",
        "## Archive search over HTTP",
        f"POST {base_url}/v1/search  ->  SearchOutput (a `data` array of hits).",
        'Body: {\"mode\": \"embed\"|\"attribute\"|\"fusion\"|\"object\",',
        '       \"query\": \"...\", \"top_k\": 10,',
        '       \"source_type\": \"video_file\"|\"rtsp\", \"video_sources\": [..]}',
        "Use this instead of the `vss` CLI when uv or a repo checkout is absent;",
        "it runs the same command host-side. Present hits as prose, not raw JSON.",
        "",
        "## Conventions",
        "- Each skill lists what it needs. Before using one, verify those tools",
        "  exist (`command -v <tool>`). If something is missing, say exactly which",
        "  tool is absent -- do not blame an unrelated service.",
        "- `vss-repo` means the skill needs a VSS source checkout and its CLI; that",
        "  is usually absent in a sandbox, and no amount of network access fixes it.",
        "- Deployment/teardown goes through the VSS Orchestrator MCP, never raw",
        "  `docker compose` or host shell commands. That MCP runs on the host, so",
        "  you do not need local docker to deploy.",
        "- Report progress in chat as you go; do not go silent during long tasks.",
        "- Never invent a host:port URL for the user; read the deployed public",
        "  origin from the deployment rather than constructing one.",
        "",
        "---",
        "",
    ]
    return "\n".join(lines)


class SentinelFilter:
    """Strip sentinel tokens from a token stream without breaking streaming.

    Holds back the last `_HOLD` characters so a sentinel split across two
    deltas ("NO_" + "REPLY") is still caught, then flushes the remainder.
    """

    def __init__(self):
        self.buf = ""

    def feed(self, text: str) -> str:
        # Strip the whole buffer BEFORE splitting: stripping only the emitted
        # prefix lets a sentinel spanning the emit/holdback boundary escape.
        self.buf = self._strip(self.buf + text)
        if len(self.buf) <= _HOLD:
            return ""
        emit, self.buf = self.buf[:-_HOLD], self.buf[-_HOLD:]
        return emit

    def flush(self) -> str:
        out, self.buf = self._strip(self.buf), ""
        return out

    @staticmethod
    def _strip(text: str) -> str:
        for token in SENTINELS:
            text = text.replace(token, "")
        return text


def sse(data: str) -> bytes:
    return f"data: {data}\n\n".encode()


def run_search(body):
    """Invoke `vss search run <mode> --raw` and return its SearchOutput.

    Args are built as a list and passed without a shell, and every value is
    validated or coerced, so a request body cannot inject flags or commands.
    """
    mode = body.get("mode", "embed")
    if mode not in SEARCH_MODES:
        return 400, {"error": f"mode must be one of {list(SEARCH_MODES)}"}
    if not os.path.isdir(VSS_REPO_ROOT):
        return 503, {"error": f"VSS_REPO_ROOT not found: {VSS_REPO_ROOT}"}
    if not os.path.exists(UV_BIN):
        return 503, {"error": "uv not found on the host; set UV_BIN"}

    cmd = [UV_BIN, "run", "--project", os.path.join(VSS_REPO_ROOT, "services", "agent"),
           "--no-dev", "--extra", "cli", "vss", "search", "run", mode, "--raw"]

    query = body.get("query")
    if query:
        cmd += ["--query", str(query)]
    try:
        top_k = int(body.get("top_k", 10))
    except (TypeError, ValueError):
        return 400, {"error": "top_k must be an integer"}
    cmd += ["--top-k", str(max(1, min(top_k, 1000)))]
    if body.get("source_type") in ("video_file", "rtsp"):
        cmd += ["--source-type", body["source_type"]]
    for src in body.get("video_sources") or []:
        cmd += ["--video-source", str(src)]
    for key, flag in (("timestamp_start", "--timestamp-start"),
                      ("timestamp_end", "--timestamp-end"),
                      ("object_id", "--object-id")):
        if body.get(key):
            cmd += [flag, str(body[key])]

    try:
        proc = subprocess.run(cmd, cwd=VSS_REPO_ROOT, capture_output=True,
                              text=True, timeout=SEARCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 504, {"error": f"search timed out after {SEARCH_TIMEOUT}s"}
    if proc.returncode != 0:
        return 502, {"error": "search command failed",
                     "exit_code": proc.returncode,
                     "stderr": (proc.stderr or "")[-800:]}
    # The CLI prefixes log lines; SearchOutput is the last JSON object printed.
    out = (proc.stdout or "").strip()
    start = out.find("{")
    if start == -1:
        return 502, {"error": "no JSON in search output", "stdout": out[-500:]}
    try:
        return 200, json.loads(out[start:])
    except json.JSONDecodeError as exc:
        return 502, {"error": f"unparseable search output: {exc}",
                     "stdout": out[-500:]}


BOOTSTRAPPED = set()
_BOOTSTRAP_LOCK = threading.Lock()


def run_turn(message: str, session_key: str, out: queue.Queue):
    """Drive one agent turn, pushing SSE-ready strings onto `out`."""
    ws = None
    try:
        ws = websocket.create_connection(GATEWAY_URL, timeout=15)
        ws.settimeout(TURN_TIMEOUT)
        ws.recv()  # connect.challenge

        def req(method, params):
            rid = str(uuid.uuid4())
            ws.send(json.dumps({"type": "req", "id": rid, "method": method, "params": params}))
            return rid

        connect_id = req("connect", {
            "minProtocol": 4, "maxProtocol": 4,
            "client": {"id": "openclaw-control-ui", "version": "vss-adapter-0.1",
                       "platform": "node", "mode": "webchat"},
            "role": "operator",
            "scopes": ["operator.read", "operator.write"],
            "caps": ["tool-events"],
            "auth": {"token": GATEWAY_TOKEN},
            "userAgent": "vss-agent-adapter", "locale": "en-US",
        })
        # The gateway rejects any request that arrives before `connect`
        # completes ("invalid handshake: first request must be connect"),
        # so await the handshake response rather than firing off in parallel.
        while True:
            hello = json.loads(ws.recv())
            if hello.get("type") == "res" and hello.get("id") == connect_id:
                if not hello.get("ok"):
                    raise RuntimeError(f"connect failed: {hello.get('error')}")
                break

        # Fresh, isolated session per conversation: reusing agent:main:main
        # inherits heartbeat/main-session history and derails the reply.
        req("sessions.create", {"key": session_key})
        req("sessions.messages.subscribe", {"key": session_key})
        # Prepend deployment context on a session's first turn only. Sending it
        # as its own turn would make the agent reply to it and surface a stray
        # message to the user.
        with _BOOTSTRAP_LOCK:
            first_turn = session_key not in BOOTSTRAPPED
            BOOTSTRAPPED.add(session_key)
        if first_turn and BOOTSTRAP_ENABLED:
            message = BOOTSTRAP_TEXT() + message

        req("chat.send", {"sessionKey": session_key, "message": message,
                          "idempotencyKey": str(uuid.uuid4())})

        step = 0
        sentinels = SentinelFilter()
        while True:
            msg = json.loads(ws.recv())
            if msg.get("type") == "res" and msg.get("ok") is False:
                err = msg.get("error", {})
                out.put(sse(json.dumps({"choices": [{"delta": {
                    "content": f"\n[adapter] gateway error: {err.get('message')}"}}]})))
                break
            if msg.get("type") != "event":
                continue
            ev, p = msg.get("event"), msg.get("payload", {}) or {}

            if ev == "chat":
                if p.get("state") == "delta" and p.get("deltaText"):
                    clean = sentinels.feed(p["deltaText"])
                    if clean:
                        out.put(sse(json.dumps(
                            {"choices": [{"delta": {"content": clean}}]})))
                elif p.get("state") == "final":
                    tail = sentinels.flush()
                    if tail:
                        out.put(sse(json.dumps(
                            {"choices": [{"delta": {"content": tail}}]})))
                    break
            elif ev == "agent" and p.get("stream") == "tool":
                # Surface tool activity as NAT-UI intermediate steps.
                d = p.get("data", {}) or {}
                step += 1
                out.put(("intermediate_data: " + json.dumps({
                    "id": str(p.get("seq", step)), "status": "in_progress",
                    "name": d.get("name") or d.get("tool") or "tool",
                    "payload": json.dumps(d)[:800],
                    "parent_id": "default", "index": step,
                }) + "\n").encode())
    except Exception as exc:  # noqa: BLE001 - POC: surface everything to the UI
        out.put(sse(json.dumps({"choices": [{"delta": {
            "content": f"\n[adapter] {type(exc).__name__}: {exc}"}}]})))
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        out.put(sse("[DONE]"))
        out.put(None)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"[adapter] {self.address_string()} {fmt % args}", flush=True)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj, indent=1).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _skills_manifest(self):
        return skills_manifest()

    def _tgz(self, members, filename):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for arcname, src in members.items():
                tar.add(src, arcname=arcname)
        data = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         f'attachment; filename="{filename}"')
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"

        if path == "/v1/skills":
            self._json({"count": len(self._skills_manifest()),
                        "skills": self._skills_manifest()})
            return

        if path == "/v1/skills/env":
            # Where VSS actually is, so a BYO agent does not have to re-derive
            # host resolution from prose in deployment_resolution.md / ENV.md.
            self._json({
                "host_alias": HOST_ALIAS,
                "note": "In-sandbox only. Never use localhost or a literal IP; "
                        "the egress policy whitelists this alias on fixed ports.",
                "services": {
                    "vss_agent": f"http://{HOST_ALIAS}:8000",
                    "orchestrator_mcp": f"http://{HOST_ALIAS}:9988/mcp",
                    "va_mcp": f"http://{HOST_ALIAS}:9901",
                    "alert_bridge": f"http://{HOST_ALIAS}:9080",
                    "elasticsearch": f"http://{HOST_ALIAS}:9200",
                    "vst_vios": f"http://{HOST_ALIAS}:30888",
                    "rt_vlm": f"http://{HOST_ALIAS}:8018",
                    "archive_search": f"{ADAPTER_PUBLIC_URL}/v1/search",
                },
            })
            return

        if path.startswith("/v1/skills/") and path != "/v1/skills/env":
            rest = path[len("/v1/skills/"):]
            want_bundle = rest.endswith("/bundle.tar.gz")
            name = rest[:-len("/bundle.tar.gz")] if want_bundle else rest
            entry = next((e for e in skills_manifest() if e["name"] == name), None)
            if entry:
                skill_dir = os.path.join(SKILLS_DIR, entry["path"])
                if want_bundle:
                    self._tgz({name: skill_dir}, f"{name}.tar.gz")
                    return
                with open(os.path.join(skill_dir, "SKILL.md"),
                          encoding="utf-8", errors="replace") as fh:
                    body = fh.read().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self._cors()
                self.end_headers()
                self.wfile.write(body)
                return
            if name != "bundle.tar.gz":
                self._json({"error": f"unknown skill: {name}"}, 404)
                return

        if path == "/v1/skills/bundle.tar.gz":
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                for e in self._skills_manifest():
                    tar.add(os.path.join(SKILLS_DIR, e["path"]),
                            arcname=e["name"])
            data = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition",
                             'attachment; filename="vss-skills.tar.gz"')
            self._cors()
            self.end_headers()
            self.wfile.write(data)
            return

        if path != "/health":
            self.send_error(404)
            return
        self._json({"ok": True, "gateway": GATEWAY_URL,
                    "sessionPrefix": SESSION_PREFIX,
                    "skills": len(self._skills_manifest())})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path not in ("/chat/stream", "/generate/stream", "/v1/search"):
            self.send_error(404)
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self.send_error(400, "invalid JSON")
            return

        if path == "/v1/search":
            code, payload = run_search(body)
            self._json(payload, code)
            return

        msgs = body.get("messages") or []
        user = next((m.get("content") for m in reversed(msgs)
                     if m.get("role") == "user"), None) or body.get("input_message")
        if not user:
            self.send_error(400, "no user message")
            return
        if isinstance(user, list):  # OpenAI content-parts form
            user = " ".join(part.get("text", "") for part in user
                            if isinstance(part, dict))

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()

        # One session per UI conversation; header lets the UI keep continuity.
        conv = (self.headers.get("Conversation-Id")  # sent natively by the VSS UI
                or self.headers.get("X-VSS-Session")
                or body.get("conversation_id"))
        session_key = f"{SESSION_PREFIX}-{conv or uuid.uuid4().hex[:12]}"

        out: queue.Queue = queue.Queue()
        threading.Thread(target=run_turn, args=(user, session_key, out),
                         daemon=True).start()
        while True:
            chunk = out.get()
            if chunk is None:
                break
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                print("[adapter] client disconnected", flush=True)
                break


if __name__ == "__main__":
    if not GATEWAY_TOKEN:
        raise SystemExit("OPENCLAW_GATEWAY_TOKEN is required")
    print(f"[adapter] listening on :{LISTEN_PORT} -> {GATEWAY_URL} ({SESSION_PREFIX}-*)",
          flush=True)
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()
