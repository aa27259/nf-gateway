#!/usr/bin/env python3
"""neuroflash -> OpenAI-compatible gateway. Zero deps (stdlib-only).

Usage:
    export NF_CLIENT_ID="your-service-account-id"
    export NF_CLIENT_SECRET="your-secret"
    python3 nf_gate.py [--port 8090] [--selftest]

Workspace = auto-discovered. Token auto-refreshes (one retry on 401).
Tool calling = native passthrough to Claude/GPT/Gemini.

neuroflash's hosting hard-closes any single action (usually long reasoning)
after ~150s. Both response modes survive that:
  stream=true  -> partial text is spliced-continued into the SAME client stream
  stream=false -> the truncated body is re-fetched over SSE so partial text is
                  salvaged instead of dying as a JSON parse error
If nothing can be salvaged the stream ends with finish_reason="length" (never a
fake normal finish), and non-stream answers HTTP 504.

Endpoints:
  GET  /v1/models      (?all=1 includes unavailable)
  POST /v1/chat/completions   (stream:true -> SSE)
  GET  /health
"""

import os, sys, json, time, threading, urllib.request, urllib.error, urllib.parse, http.client, http.server

# 8088 is commonly taken (e.g. by other local proxies); 8090 is what OMP expects.
PORT = 8090
for _i, _a in enumerate(sys.argv):
    if _a == "--port" and _i + 1 < len(sys.argv): PORT = int(sys.argv[_i + 1])

CID = os.environ.get("NF_CLIENT_ID", "")
SEC = os.environ.get("NF_CLIENT_SECRET", "")
WSID = os.environ.get("NF_WORKSPACE_ID", "")
MODEL_MAP_RAW = os.environ.get("NF_MODEL_MAP", "")
DEFAULT_MODEL = os.environ.get("NF_DEFAULT_MODEL", "claude-sonnet-5.5")
# default reasoning effort when the client sends none; empty = upstream default
REASONING_EFFORT = os.environ.get("NF_REASONING_EFFORT", "").strip()
# upstream hard-closes streams after ~150s; auto-continue a cut reply up to N times
MAX_CONTINUE = int(os.environ.get("NF_MAX_CONTINUE", "5") or 0)
# Upstream's accepted reasoning_effort enum (authoritative, from its own 400):
#   max | xhigh | high | medium | low | minimal | none
# Cut before any text appeared (long reasoning) -> retry one step lower.
# A client that sent no effort is treated as high, so the first retry is medium.
LOWER_EFFORT = {None: "medium", "max": "xhigh", "xhigh": "high", "high": "medium",
                "medium": "low", "low": "minimal"}

def _effort(nf):
    r = nf.get("reasoning")
    return nf.get("reasoning_effort") or (r.get("effort") if isinstance(r, dict) else None)

CONT_PROMPT = ("Your previous reply was cut off by a connection time limit. Continue exactly from the point "
               "where it stopped. Do not repeat anything already written and do not add any preamble.")

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

def _new_st():
    return {"content": "", "tools": False, "finish": None, "usage": None, "model": None}

def _consume(r, st, sink=None):
    """Read one upstream SSE response, accumulating into st. Forward each event to
    `sink` when given. Returns True only on a clean upstream finish.

    Pure w.r.t. the network (takes an already-open stream) so the truncation
    logic is testable without burning quota -- see selftest().
    """
    while True:
        try: raw = r.readline()
        except (OSError, http.client.HTTPException): return False  # upstream dropped mid-frame
        if not raw: break                                          # EOF without [DONE]
        l = raw.decode("utf-8", "replace")
        s = l.strip()
        if s == "data: [DONE]": return st["finish"] is not None or True
        if s.startswith("data: "):
            try: c = json.loads(s[6:])
            except ValueError: c = None
            if isinstance(c, dict):
                if "model" in c:
                    c["model"] = strip_provider(c["model"])
                    st["model"] = c["model"]
                if c.get("usage"): st["usage"] = c["usage"]
                for ch in c.get("choices") or []:
                    d = ch.get("delta") or {}
                    st["content"] += d.get("content") or ""
                    if d.get("tool_calls"): st["tools"] = True
                    if ch.get("finish_reason"): st["finish"] = ch["finish_reason"]
                l = f"data: {json.dumps(c)}\n\n"
        if sink: sink(l)
    return st["finish"] is not None

def _ladder(nf, st, sink, log):
    """Drive upstream with auto-continue (cut mid-text) and effort downgrade (cut
    while still reasoning). Shared by both response modes. Returns True on a clean
    finish; False means the reply is truncated and the caller must say so."""
    tries = 0
    while True:
        try:
            with call(CHAT, nf, accept="text/event-stream") as r:
                if _consume(r, st, sink): return True
        except urllib.error.HTTPError:
            raise  # quota / permission / upstream 5xx: not a truncation, don't retry
        if st["tools"]: return False  # partial tool_calls cannot be spliced safely
        if tries >= MAX_CONTINUE: return False
        tries += 1
        if st["content"]:
            log(f"upstream cut at {len(st['content'])} chars -> continue #{tries}")
            msgs = list(nf["messages"]) + [{"role": "assistant", "content": st["content"]},
                                           {"role": "user", "content": CONT_PROMPT}]
            nf = dict(nf, messages=msgs)
        else:
            lower = LOWER_EFFORT.get(_effort(nf))
            if not lower:
                log(f"upstream cut while reasoning, effort {_effort(nf) or 'default'} is at the floor -> giving up")
                return False
            log(f"upstream cut while reasoning ({_effort(nf) or 'default'}) -> retry with effort={lower}")
            nf = {k: v for k, v in nf.items() if k != "reasoning"}
            nf["reasoning_effort"] = lower

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args): pass

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

    @staticmethod
    def _log(m): print(m, flush=True)

    def _salvage_nonstream(self, nf, model):
        """The plain non-stream call came back truncated (upstream's ~150s cut), so
        there is no parseable body. Re-fetch over SSE to keep whatever text did
        arrive, then run the same continue/downgrade ladder. Returns an OpenAI
        chat.completion dict, or None when nothing at all could be salvaged."""
        st = _new_st()
        done = _ladder(dict(nf, stream=True), st, None, self._log)
        if not st["content"]: return None
        res = {"id": f"nf-{int(time.time() * 1000)}", "object": "chat.completion",
               "created": int(time.time()), "model": st["model"] or strip_provider(model),
               "choices": [{"index": 0, "message": {"role": "assistant", "content": st["content"]},
                            "finish_reason": st["finish"] or ("stop" if done else "length")}]}
        if st["usage"]: res["usage"] = st["usage"]
        self._log(f"non-stream salvaged {len(st['content'])} chars, done={done} "
                  f"finish={res['choices'][0]['finish_reason']}")
        return res

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
        asked = strip_provider(body.get("model") or DEFAULT_MODEL)
        model = MODEL_MAP.get(asked, asked)
        nf = {"model": model, "messages": body.get("messages", []), "stream": stream}
        for k in ("temperature","top_p","max_tokens","max_completion_tokens","stop",
                  "tools","tool_choice","response_format","seed",
                  "presence_penalty","frequency_penalty","n","user",
                  "reasoning_effort","reasoning","verbosity","parallel_tool_calls","stream_options"):
            if k in body: nf[k] = body[k]
        if REASONING_EFFORT and "reasoning_effort" not in nf and "reasoning" not in nf:
            nf["reasoning_effort"] = REASONING_EFFORT
        print(f"req {model} stream={stream} msgs={len(nf['messages'])} tools={len(nf.get('tools') or [])} "
              f"effort={_effort(nf) or 'default'}", flush=True)

        hs = False  # headers_sent flag
        try:
            if not stream:
                try:
                    with call(CHAT, nf) as r:
                        res = json.loads(r.read())
                except (ValueError, OSError, http.client.HTTPException) as e:
                    # truncated body / dropped connection: salvage, don't 500 on a parse error
                    self._log(f"non-stream truncated upstream ({type(e).__name__}: {str(e)[:120]}) -> salvage")
                    res = self._salvage_nonstream(nf, model)
                    if res is None:
                        self._err(504, "upstream closed the response before any content arrived "
                                       "(neuroflash hosting time limit) and retries produced nothing")
                        return
                if "model" in res: res["model"] = strip_provider(res["model"])
                self._send(200, json.dumps(res))
                return

            # streaming SSE -- Connection: close for HTTP/1.1 proper framing
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self._cors(); self.end_headers()
            self.close_connection = True
            hs = True

            st = _new_st()
            def sink(l):
                self.wfile.write(l.encode()); self.wfile.flush()
            done = _ladder(nf, st, sink, self._log)
            if not done:
                # still cut: tell the client honestly instead of faking a normal finish
                self._log(f"upstream cut (tools={st['tools']}, chars={len(st['content'])}) -> finish_reason=length")
                cut = {"id": "nf-cut", "object": "chat.completion.chunk", "created": int(time.time()),
                       "model": st["model"] or model,
                       "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}
                self.wfile.write(f"data: {json.dumps(cut)}\n\n".encode())
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

def selftest():
    """Runnable check on exactly the logic that broke: SSE truncation handling and
    the reasoning-effort downgrade ladder. No network, no quota."""
    import io
    def frames(*b): return io.BytesIO(b"".join(b))

    # 1. effort ladder follows upstream's real enum, never loops, terminates at the floor
    chain, cur = ["max"], "max"
    while True:
        nxt = LOWER_EFFORT.get(cur)
        if not nxt: break
        assert nxt not in chain, f"effort ladder loops at {cur}"
        chain.append(nxt); cur = nxt
    assert chain == ["max", "xhigh", "high", "medium", "low", "minimal"], chain
    assert LOWER_EFFORT.get(None) == "medium", "no-effort client must first retry at medium"
    assert LOWER_EFFORT.get("minimal") is None, "minimal is the floor"

    # 2. clean stream: content accumulates, provider prefix stripped, finish seen
    st = _new_st()
    ok = _consume(frames(
        b'data: {"model":"anthropic/claude-x","choices":[{"index":0,"delta":{"content":"Hel"}}]}\n\n',
        b'data: {"choices":[{"index":0,"delta":{"content":"lo"}}]}\n\n',
        b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"cost":0.1}}\n\n',
        b'data: [DONE]\n\n'), st, None)
    assert ok is True, ok
    assert st["content"] == "Hello", st
    assert st["finish"] == "stop" and st["model"] == "claude-x" and st["usage"] == {"cost": 0.1}, st

    # 3. THE bug: upstream cut mid-frame -> False, but partial text is kept for salvage
    st = _new_st()
    ok = _consume(frames(b'data: {"choices":[{"index":0,"delta":{"content":"partial answer"}}]}\n\n'), st, None)
    assert ok is False, "a stream with no [DONE] and no finish_reason must not report success"
    assert st["content"] == "partial answer", st
    assert st["finish"] is None

    # 4. finish_reason present but no [DONE] (EOF) still counts as a clean finish
    st = _new_st()
    assert _consume(frames(b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'), st, None) is True

    # 5. tool_calls are flagged (the ladder must not splice those)
    st = _new_st()
    _consume(frames(b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0}]}}]}\n\ndata: [DONE]\n\n'), st, None)
    assert st["tools"] is True

    # 6. sink sees re-serialized frames with the provider prefix already stripped
    out = []
    _consume(frames(b'data: {"model":"openai/gpt-6-luna","choices":[{"index":0,"delta":{"content":"x"}}]}\n\n'),
             _new_st(), out.append)
    assert out and "gpt-6-luna" in out[0] and "openai/" not in out[0], out

    # 7. malformed JSON inside a frame must not kill the stream
    st = _new_st()
    _consume(frames(b'data: {not json}\n\n',
                    b'data: {"choices":[{"index":0,"delta":{"content":"survived"}}]}\n\n',
                    b'data: [DONE]\n\n'), st, None)
    assert st["content"] == "survived", st

    print(f"selftest OK: 7 checks (effort ladder {chain})")

def main():
    if "--selftest" in sys.argv:
        selftest(); return
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
    print(f"reasoning_effort default: {REASONING_EFFORT or 'upstream'}, auto-continue: {MAX_CONTINUE}")
    print(f"\ngateway on :{PORT} ({DEFAULT_MODEL})")

    srv = http.server.ThreadingHTTPServer((os.environ.get("NF_HOST", "127.0.0.1"), PORT), Handler)
    srv.daemon_threads = True
    try: srv.serve_forever()
    except KeyboardInterrupt: print("\nstopped"); srv.shutdown()

if __name__ == "__main__":
    main()
