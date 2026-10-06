#!/usr/bin/env python3
"""neuroflash → OpenAI-compatible gateway with tool use emulation.

Two auth modes (auto-detect):
  Session: set NF_TOKEN (JWT from nf-access-token cookie) → Claude Opus 5.5
  OAuth2:  set NF_CLIENT_ID + NF_CLIENT_SECRET → free 5k credits/day

Usage:
  export NF_TOKEN="eyJ..."; export NF_WORKSPACE_ID="0f04..."; python3 nf_gate.py
  # or
  export NF_CLIENT_ID="your-client-id"
  export NF_CLIENT_SECRET="your-client-secret"
  export NF_WORKSPACE_ID="your-workspace-id"
  python3 nf_gate.py [--port 8088]
"""

import os, sys, json, time, http.server, urllib.request, urllib.error, urllib.parse, socket, threading, re

# ─── config ──────────────────────────────────────────────────────────────────────────
PORT = 8088
for i, a in enumerate(sys.argv):
    if a == "--port" and i + 1 < len(sys.argv):
        PORT = int(sys.argv[i + 1])

TOKEN = os.environ.get("NF_TOKEN", "")                          # session JWT cookie
CID = os.environ.get("NF_CLIENT_ID", "")                          # OAuth2 service account
SEC = os.environ.get("NF_CLIENT_SECRET", "")                     # OAuth2 secret
WSID = os.environ.get("NF_WORKSPACE_ID", "")
MODEL_MAP = os.environ.get("NF_MODEL_MAP", "")                   # alias:real,alias2:real2

TOKEN_URL = "https://id.neuroflash.com/oauth/v2/token"
API_BASE = "https://app.neuroflash.com/api"

# cache
_oauth_token = None
_oauth_expires = 0

# ─── auth ──────────────────────────────────────────────────────────────────────────────
def bearer():
    global _oauth_token, _oauth_expires
    if TOKEN and not TOKEN.startswith("$$"):
        return TOKEN, "session"
    if CID and SEC:
        if not _oauth_token or time.time() > _oauth_expires - 120:
            data = urllib.parse.urlencode(dict(
                grant_type="client_credentials", client_id=CID, client_secret=SEC, scope="openid"
            )).encode()
            req = urllib.request.Request(TOKEN_URL, data=data,
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = json.loads(r.read())
                _oauth_token = body["access_token"]
                _oauth_expires = time.time() + body.get("expires_in", 14399)
            except Exception as e:
                raise RuntimeError(f"OAuth2 token exchange failed: {e}")
        return _oauth_token, "oauth2"
    raise RuntimeError("Set NF_TOKEN (session) or NF_CLIENT_ID+NF_CLIENT_SECRET (OAuth2)")

# ─── API calls ────────────────────────────────────────────────────────────────────────
def headers():
    tok, kind = bearer()
    h = {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://app.neuroflash.com",
        "Referer": "https://app.neuroflash.com/",
    }
    if WSID:
        h["x-workspace-id"] = WSID
    return h

def api_get(path):
    req = urllib.request.Request(f"{API_BASE}{path}", headers=headers())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def api_post(path, body, stream=False):
    data = json.dumps(body).encode()
    h = headers()
    if stream:
        h["Accept"] = "text/event-stream"
    req = urllib.request.Request(f"{API_BASE}{path}", data=data, headers=h)
    r = urllib.request.urlopen(req, timeout=600)
    return r

def api_post_stream(path, body):
    return api_post(path, body, stream=True)

# ─── tool calling emulation ──────────────────────────────────────────────────────────
TOOL_SYSTEM_TPL = """You have access to the following tools. When the user asks you to do something that requires a tool, respond with EXACTLY this JSON format (no markdown, no other text):

{"function": "tool_name", "arguments": {"arg1": "val1", ...}}

Tools available:
{tools_desc}

If no tool is needed, respond normally with text.

IMPORTANT: Your response must be ONLY the JSON above if calling a tool, or ONLY natural text if not calling a tool. Never mix both."""

TOOL_RE = re.compile(r'\{"function":\s*"([^"]+)"\s*,\s*"arguments":\s*(\{.*?\})\s*\}', re.DOTALL)

def _build_tools_desc(tools):
    lines = []
    for t in tools:
        name = t.get("function", {}).get("name", "?")
        desc = t.get("function", {}).get("description", "")
        params = t.get("function", {}).get("parameters", {})
        lines.append(f"- {name}: {desc}")
        props = params.get("properties", {})
        reqs = params.get("required", [])
        for pname, pinfo in props.items():
            r = " (required)" if pname in reqs else ""
            lines.append(f"  - {pname} ({pinfo.get('type', 'string')}): {pinfo.get('description', '')}{r}")
    return "\n".join(lines)

def _emulate_tool_call(response_text, tools):
    """Parse a potential tool call from model response."""
    m = TOOL_RE.search(response_text)
    if not m:
        return None
    name = m.group(1)
    try:
        args = json.loads(m.group(2))
    except json.JSONDecodeError:
        return None
    # verify tool exists
    for t in tools:
        if t.get("function", {}).get("name") == name:
            return {"name": name, "arguments": json.dumps(args)}
    return None

# ─── model list ─────────────────────────────────────────────────────────────────────────
def get_models():
    try:
        ms = api_get("/ds-prototypes/model_selection/models")
    except Exception:
        ms = []
    out = [{"id": m["id"], "object": "model", "created": int(time.time()),
            "owned_by": m.get("provider", "nf"), "available": m.get("available", True)}
           for m in ms if isinstance(m, dict)]
    if MODEL_MAP:
        for pair in MODEL_MAP.split(","):
            a, r = pair.strip().split(":")
            out.append({"id": a, "object": "model", "created": int(time.time()),
                        "owned_by": f"alias:{r}", "available": True})
    # dedupe
    seen = set()
    deduped = []
    for m in out:
        if m["id"] not in seen:
            seen.add(m["id"])
            deduped.append(m)
    return deduped

# ─── OpenAI HTTP handler ────────────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _hdr(self, k, d=""):
        return self.headers.get(k, d)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")

    def _send(self, code, body, ct="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self._cors()
        self.end_headers()
        b = body.encode() if isinstance(body, str) else body
        self.wfile.write(b)

    def _read_body(self):
        size = int(self._hdr("Content-Length", 0))
        return json.loads(self.rfile.read(size) if size else b"{}")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/v1/models":
            try:
                self._send(200, json.dumps({"object": "list", "data": get_models()}))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif self.path == "/health":
            self._send(200, json.dumps({"ok": True, "auth": "session" if TOKEN else "oauth2"}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path not in ("/v1/chat/completions", "/v1/chat/completions/stream"):
            self._send(404, json.dumps({"error": "not found"}))
            return
        try:
            body = self._read_body()
        except Exception:
            self._send(400, json.dumps({"error": "invalid json"}))
            return

        stream = body.get("stream", False) or "/stream" in self.path
        nf_body = {
            "model": body.get("model", "gpt-4.1-mini"),
            "messages": body.get("messages", []),
            "temperature": body.get("temperature", 0.7),
            "max_tokens": body.get("max_tokens", 4096),
            "stream": stream,
        }
        # model alias reverse
        if MODEL_MAP:
            rev = {}
            for p in MODEL_MAP.split(","):
                a, r = p.strip().split(":")
                rev[a] = r
            nf_body["model"] = rev.get(nf_body["model"], nf_body["model"])

        # tool use emulation
        tools = body.get("tools", [])
        tool_choice = body.get("tool_choice", "auto")

        # inject tools as system message
        if tools:
            sys_idx = None
            for i, m in enumerate(nf_body["messages"]):
                if m.get("role") == "system":
                    sys_idx = i
                    break
            tool_block = {
                "role": "system",
                "content": TOOL_SYSTEM_TPL.format(tools_desc=_build_tools_desc(tools))
            }
            if sys_idx is not None:
                nf_body["messages"].insert(sys_idx + 1, tool_block)
            else:
                nf_body["messages"].insert(0, tool_block)

        try:
            data = json.dumps(nf_body).encode()
            h = headers()
            if stream:
                h["Accept"] = "text/event-stream"

            req = urllib.request.Request(
                f"{API_BASE}/ds-prototypes/content_generation/chat/completions",
                data=data, headers=h)
            resp = urllib.request.urlopen(req, timeout=600)

            if not stream:
                result = json.loads(resp.read())
                # check for tool call in response
                if tools and "choices" in result:
                    content = result["choices"][0].get("message", {}).get("content", "")
                    tc = _emulate_tool_call(content, tools)
                    if tc:
                        result["choices"][0]["message"]["content"] = None
                        result["choices"][0]["message"]["tool_calls"] = [{
                            "id": f"call_{int(time.time())}",
                            "type": "function",
                            "function": tc,
                        }]
                self._send(200, json.dumps(result))
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self._cors()
                self.end_headers()
                # proxy SSE, detect tool call in final chunk
                buffer = ""
                for line in resp:
                    decoded = line.decode("utf-8", "replace")
                    self.wfile.write(decoded.encode())
                    self.wfile.flush()
                    if decoded.startswith("data: ") and decoded.strip() != "data: [DONE]":
                        try:
                            chunk = json.loads(decoded[6:])
                            if chunk.get("choices") and chunk["choices"][0].get("delta", {}).get("content"):
                                buffer += chunk["choices"][0]["delta"]["content"]
                        except json.JSONDecodeError:
                            pass
                # check for tool call in complete streamed response
                if tools and buffer:
                    tc = _emulate_tool_call(buffer, tools)
                    if tc:
                        # send a tool_calls delta before [DONE]
                        tc_chunk = json.dumps({
                            "id": f"chatcmpl-{int(time.time())}",
                            "object": "chat.completion.chunk",
                            "choices": [{
                                "index": 0,
                                "delta": {},
                                "finish_reason": "tool_calls",
                            }]
                        })
                        self.wfile.write(f"data: {tc_chunk}\n\n".encode())
                    else:
                        replace = json.dumps({
                            "id": f"chatcmpl-{int(time.time())}",
                            "object": "chat.completion.chunk",
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                        })
                        self.wfile.write(f"data: {replace}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        except urllib.error.HTTPError as e:
            err = e.read()
            ct = e.headers.get("Content-Type", "application/json")
            self._send(e.code, err, ct)
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}))


def main():
    has_session = bool(TOKEN) and not TOKEN.startswith("$$")
    has_oauth = bool(CID) and bool(SEC)
    has_ws = bool(WSID)

    if not has_session and not has_oauth:
        print("""ERROR: no auth configured.
  Session: export NF_TOKEN='eyJ...'  (JWT from nf-access-token cookie)
  OAuth2:  export NF_CLIENT_ID='your-client-id'
           export NF_CLIENT_SECRET='...'
           export NF_WORKSPACE_ID='...' (from app cookies)""")
        sys.exit(1)
    if not has_ws:
        print("WARN: NF_WORKSPACE_ID not set — some endpoints may fail")

    auth_type = "session" if has_session else "oauth2"
    print(f"neuroflash gateway :{PORT} ({auth_type} auth)")
    if has_session: print("  models: includes any session-accessible models (Claude Opus 5.5!)")
    if MODEL_MAP: print(f"  model aliases: {MODEL_MAP}")
    print(f"  workspace: {WSID or 'not set'}")
    print("  endpoints: GET /v1/models, POST /v1/chat/completions, GET /health")

    try:
        ms = get_models()
        print(f"  models available: {[m['id'] for m in ms[:8]]}" + (f" +{len(ms)-8} more" if len(ms) > 8 else ""))
    except Exception as e:
        print(f"  models: fetch failed ({e}) — check credentials")

    srv = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")

if __name__ == "__main__":
    main()