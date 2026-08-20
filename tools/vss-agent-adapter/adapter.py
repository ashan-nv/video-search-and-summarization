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
import json, os, queue, threading, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import websocket

GATEWAY_URL = os.environ.get("OPENCLAW_GATEWAY_URL", "ws://localhost:18789/")
GATEWAY_TOKEN = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")
SESSION_PREFIX = os.environ.get("OPENCLAW_SESSION_PREFIX", "agent:main:vss")
LISTEN_PORT = int(os.environ.get("ADAPTER_PORT", "9099"))
TURN_TIMEOUT = int(os.environ.get("ADAPTER_TURN_TIMEOUT", "600"))


def sse(data: str) -> bytes:
    return f"data: {data}\n\n".encode()


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
        req("chat.send", {"sessionKey": session_key, "message": message,
                          "idempotencyKey": str(uuid.uuid4())})

        step = 0
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
                    out.put(sse(json.dumps(
                        {"choices": [{"delta": {"content": p["deltaText"]}}]})))
                elif p.get("state") == "final":
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

    def do_GET(self):
        if self.path.rstrip("/") != "/health":
            self.send_error(404)
            return
        body = json.dumps({"ok": True, "gateway": GATEWAY_URL,
                           "sessionPrefix": SESSION_PREFIX}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.split("?")[0].rstrip("/") not in ("/chat/stream", "/generate/stream"):
            self.send_error(404)
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self.send_error(400, "invalid JSON")
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
