#!/usr/bin/env python3
"""neuroflash -> OpenAI-compatible gateway. Zero deps (stdlib-only).

Usage:
    export NF_CLIENT_ID="your-service-account-id"
    export NF_CLIENT_SECRET="your-secret"
    python3 nf_gate.py [--port 8088]

Workspace = auto-discovered. Token auto-refreshes (one retry on 401).
Tool calling = native passthrough to Claude/GPT/Gemini.

Endpoints:
  GET  /v1/models
  POST /v1/chat/completions  (stream:true -> SSE)
  GET  /health
"""

import os, sys, json, time, threading, urllib.request, urllib.error, urllib.parse, http.server

PORT = 8088
for _i, _a in enumerate(sys.argv):
    if _a == "--port" and _i + 1 < len(sys.argv): PORT = int(sys.argv[_i + 1])

CID = os.environ.get("NF_CLIENT_ID", "")
SEC = os.environ.get("NF_CLIENT_SECRET", "")
WSID = os.environ.get("NF_WORKSPACE_ID", "")
MODEL_MAP_RAW = os.environ.get("NF_MODEL_MAP", "")
DEFAULT_MODEL = os.environ.get("NF_DEFAULT_MODEL", "claude-sonnet-5.5")

TOKEN_URL = "https://id.neuroflash.com/oauth/v2/token"
API = "https://app.neuroflash.com/api"
CHAT = "/ds-prototypes/content_generation/chat/completions"
MODELS = "/ds-prototypes/model_selection/models"
WKS = "/workspace-service/v1/workspaces"

# model aliases (parsed once, malformed skipped)
MODEL_MAP = {}
for p in MODEL_MAP_RAW.split(","):
    p = p.strip()
    if not p: continue
    parts = p.split(":", 1)
    if len(parts) == 2 and parts[0] and parts[1]: MODEL_MAP[parts[0].strip()] = parts[1].strip()

# OAuth2 cache (thread-safe)
_lock = threading.Lock()
_token = None
_token_exp = 0.0

def _fetch_token():
    d = urllib.parse.urlencode(dict(grant_type="client_credentials", client_id=CID,
                                    client_secret=SEC, scope="openid")).encode()
    req = urllib.request.Request(TOKEN_URL, data=d,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        b = json.loads(r.read())
    if "access_token" not in b: raise RuntimeError(f"bad token response: {str(b)[:200]}")
    return b["access_token"], time.time() + int(b.get("expires_in", 14399))

def token(force=False):
    global _token, _token_exp
    with _lock:
        if force or not _token or time.time() > _token_exp - 120:
            _token, _token_exp = _fetch_token()
        return _token

def discover_ws():
    req = urllib.request.Request(f"{API}{WKS}", headers={"Authorization": f"Bearer {token()}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.loads(r.read())
    items = body.get("data", body) if isinstance(body, dict) else body
    if not items: raise RuntimeError("no workspaces found")
    return items[0]["id"]

def _headers(extra=None, ws=None):
    h = {"Authorization": f"Bearer {token()}", "Content-Type": "application/json",
         "Accept": "application/json", "Origin": "https://app.neuroflash.com",
         "Referer": "https://app.neuroflash.com/", "User-Agent": "Mozilla/5.0"}
    if ws or WSID: h["x-workspace-id"] = ws or WSID
    if extra: h.update(extra)
    return h

def call(path, body=None, accept=None, ws=None, retried=False):
    h = _headers({"Accept": accept} if accept else None, ws=ws)
    d = json.dumps(body).encode() if body is not None else None
    try:
        return urllib.request.urlopen(urllib.request.Request(f"{API}{path}", data=d, headers=h), timeout=600)
    except urllib.error.HTTPError as e:
        if e.code == 401 and not retried: token(force=True); return call(path, body, accept, ws, True)
        raise

def strip_provider(mid):
    return mid.split("/", 1)[1] if isinstance(mid, str) and "/" in mid else mid

def list_models():
    with call(MODELS) as r:
        ms = json.loads(r.read())
    out, seen = [], set()
    for m in ms if isinstance(ms, list) else []:
        if not isinstance(m, dict): continue
        mid = strip_provider(m.get("id", ""))
        if not mid or mid in seen: continue
        seen.add(mid)
        out.append({"id": mid, "object": "model", "created": int(time.time()),
                    "owned_by": m.get("provider", "neuroflash"),
                    "available": bool(m.get("available", True)),
                    "context_window": m.get("context_window")})
    for alias, real in MODEL_MAP.items():
        if alias not in seen:
            seen.add(alias)
            out.append({"id": alias, "object": "model", "created": int(time.time()),
                        "owned_by": f"alias:{real}", "available": True})
    return out

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args): pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")

    def _send(self, code, payload, ct="application/json"):
        b = payload.encode() if isinstance(payload, str) else payload
        self.send_response(code)
        self.send_header("Content-Type", ct); self.send_header("Content-Length", str(len(b)))
        self._cors(); self.end_headers()
        self.wfile.write(b)

    def _err(self, code, msg):
        self._send(code, json.dumps({"error": {"message": str(msg), "type": "gateway_error", "code": code}}))

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(n)) if n else {}

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.send_header("Content-Length", "0"); self.end_headers()

    def do_GET(self):
        p = self.path.split("?", 1)[0]
        try:
            if p == "/v1/models":
                ms = list_models()
                if "all=1" not in self.path: ms = [m for m in ms if m.get("available", True)]
                self._send(200, json.dumps({"object": "list", "data": ms}))
            elif p == "/health":
                self._send(200, json.dumps({"ok": True, "workspace": WSID or "auto"}))
            else: self._err(404, f"unknown: {p}")
        except Exception as e: self._err(500, str(e))

    def do_POST(self):
        p = self.path.split("?", 1)[0]
        if p not in ("/v1/chat/completions", "/v1/chat/completions/stream"):
            self._err(404, f"unknown: {p}"); return
        try: body = self._read_body()
        except Exception as e: self._err(400, f"bad json: {e}"); return

        stream = bool(body.get("stream", False)) or p.endswith("/stream")
        model = MODEL_MAP.get(strip_provider(body.get("model") or DEFAULT_MODEL), strip_provider(body.get("model") or DEFAULT_MODEL))
        nf = {"model": model, "messages": body.get("messages", []), "stream": stream}
        for k in ("temperature","top_p","max_tokens","max_completion_tokens","stop",
                  "tools","tool_choice","response_format","seed",
                  "presence_penalty","frequency_penalty","n","user"):
            if k in body: nf[k] = body[k]

        hs = False  # headers_sent flag
        try:
            if not stream:
                with call(CHAT, nf) as r:
                    res = json.loads(r.read())
                if "model" in res: res["model"] = strip_provider(res["model"])
                self._send(200, json.dumps(res))
                return

            # streaming SSE — Connection: close for HTTP/1.1 proper framing
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self._cors(); self.end_headers()
            self.close_connection = True
            hs = True

            saw_done = False
            with call(CHAT, nf, accept="text/event-stream") as r:
                for raw in r:
                    l = raw.decode("utf-8", "replace")
                    if l.strip() == "data: [DONE]":
                        saw_done = True
                        self.wfile.write(l.encode()); self.wfile.flush()
                        break
                    if l.startswith("data: ") and l.strip() != "data: [DONE]":
                        try:
                            c = json.loads(l[6:])
                            if "model" in c: c["model"] = strip_provider(c["model"])
                            l = f"data: {json.dumps(c)}\n\n"
                        except (json.JSONDecodeError, ValueError): pass
                    self.wfile.write(l.encode()); self.wfile.flush()
            if not saw_done:
                self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError): pass
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if not hs: self._send(e.code, detail, e.headers.get("Content-Type", "application/json"))
            else:
                try: self.wfile.write(f"data: {json.dumps({'error': detail[:300]})}\n\ndata: [DONE]\n\n".encode()); self.wfile.flush()
                except (BrokenPipeError, OSError): pass
        except Exception as e:
            if not hs: self._err(500, str(e))
            else:
                try: self.wfile.write(f"data: {json.dumps({'error': str(e)[:300]})}\n\ndata: [DONE]\n\n".encode()); self.wfile.flush()
                except (BrokenPipeError, OSError): pass

def main():
    if not CID or not SEC:
        print("ERROR: set NF_CLIENT_ID and NF_CLIENT_SECRET")
        print("  Settings -> Workspace -> API Access -> Create Service Account\n")
        sys.exit(1)
    global WSID
    try:
        token(); print("auth: OK")
    except Exception as e: print(f"ERROR: token failed: {e}"); sys.exit(1)
    if not WSID:
        try:
            WSID = discover_ws()
            print(f"workspace: auto {WSID}")
        except Exception as e: print(f"ERROR: workspace: {e}"); sys.exit(1)
    else: print(f"workspace: {WSID}")
    try:
        ms = list_models(); avail = [m for m in ms if m.get("available", True)]
        print(f"models: {len(avail)} avail of {len(ms)}")
        for m in avail[:8]: print(f"  - {m['id']}")
        if len(avail) > 8: print(f"  ... +{len(avail)-8}")
    except Exception as e: print(f"models: {e}")
    if MODEL_MAP: print(f"aliases: {MODEL_MAP}")
    print(f"\ngateway on :{PORT} ({DEFAULT_MODEL})")

    srv = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    try: srv.serve_forever()
    except KeyboardInterrupt: print("\nstopped"); srv.shutdown()

if __name__ == "__main__":
    main()