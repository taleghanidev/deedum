"""The in-process hooks. `install()` patches requests, httpx, urllib and gRPC so every call the
process makes is appended to the trail. No third-party imports at module level — the hooks attach
lazily, only if the library is already importable."""
import os, sys, json, time, uuid, fnmatch, re, threading, pathlib, datetime
from urllib.parse import urlsplit, parse_qsl
from . import store

_lock = threading.Lock()
_installed = False

class Recorder:
    def __init__(self, trail_dir, why=None, run_id=None):
        self.dir = pathlib.Path(trail_dir)
        self.cfg = store.load_config(self.dir)
        self.why = why or os.environ.get("DOCKET_WHY") or ""
        self.run = run_id or os.environ.get("DOCKET_RUN") or uuid.uuid4().hex[:8]
        self.cwd = os.getcwd()
        self.reads = 0
        self.writes = 0
        self._redact_h = {h.lower() for h in self.cfg["redact_headers"]}
        self._redact_b = {k.lower() for k in self.cfg["redact_body_keys"]}
        self._read_re = re.compile(self.cfg["read_path_regex"])
        self._auth_re = re.compile(self.cfg.get("auth_path_regex") or r"$^")
        self._scrub_re = re.compile(self.cfg.get("scrub_regex") or r"$^")
        self.argv = self.scrub_argv(" ".join(sys.argv))[:300]

    # ---- classification / redaction -------------------------------------------------------
    def ignored(self, host):
        return any(fnmatch.fnmatch(host, pat) for pat in self.cfg.get("ignore_hosts", []))

    def kind(self, method, host, path):
        for r in self.cfg.get("rules", []):
            if fnmatch.fnmatch(host, r.get("host", "*")) and re.search(r.get("path_regex", ""), path):
                return r["kind"]
        if method in ("GET", "HEAD", "OPTIONS"):
            return "read"
        if method == "POST" and self._read_re.search(path):
            return "read"
        return "write"

    def redact_headers(self, headers):
        out = {}
        for k, v in (headers or {}).items():
            out[k] = "<redacted>" if k.lower() in self._redact_h else str(v)[:500]
        return out

    def redact_obj(self, obj, depth=0):
        if depth > 30:
            return obj
        if isinstance(obj, dict):
            return {k: ("<redacted>" if str(k).lower() in self._redact_b else self.redact_obj(v, depth + 1))
                    for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.redact_obj(v, depth + 1) for v in obj]
        return obj

    _bearer_re = re.compile(r"(?i)\b(bearer|basic|token|apikey|api-key)\s+[A-Za-z0-9._~+/=-]{6,}")
    _jsonkey_re = re.compile(r'(?i)("(?:[^"]*(?:token|secret|password|passwd|key|authorization|credential)[^"]*)"\s*:\s*)"[^"]*"')
    _hdr_re = re.compile(r"(?i)((?:authorization|developertoken|developer-token|x-api-key|api-key|apikey|cookie|x-auth-token|authenticationtoken)\s*:\s*)[^\s'\"]+(?:\s+[^\s'\"]+)?")

    _flag_re = re.compile(r"(?i)(\s(?:-u|--user|--proxy-user|--oauth2-bearer|--tlsuser|--tlspassword|--token|--password|--secret|--api-key|--apikey)[= ])\S+")

    def scrub_argv(self, text):
        text = self._flag_re.sub(lambda m: m.group(1) + "<redacted>", text)
        text = self._hdr_re.sub(lambda m: m.group(1) + "<redacted>", text)
        text = self._bearer_re.sub(lambda m: m.group(1) + " <redacted>", text)
        text = self._jsonkey_re.sub(lambda m: m.group(1) + '"<redacted>"', text)
        return self.scrub(text)

    def scrub(self, text):
        return self._scrub_re.sub(lambda m: f"{m.group(1)}=<redacted>", text)

    def body(self, raw, limit):
        """bytes/str -> parsed JSON (redacted) or text, truncated to `limit` bytes."""
        if raw is None:
            return None
        if hasattr(raw, "read"):  # file-like bodies (uploads)
            return {"_stream": True}
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return {"_binary_bytes": len(raw)}
        if not isinstance(raw, str):
            raw = str(raw)
        raw = self.scrub(raw)
        size = len(raw)
        try:
            return {"_json": self.redact_obj(json.loads(raw)), **({"_bytes": size, "_truncated_after": limit} if size > limit else {})} \
                if size <= limit * 4 else {"_text": raw[:limit], "_bytes": size, "_truncated": True}
        except (ValueError, TypeError):
            pass
        if size > limit:
            return {"_text": raw[:limit], "_bytes": size, "_truncated": True}
        return {"_text": raw}

    # ---- the one write path --------------------------------------------------------------
    def record(self, rec):
        """rec: dict with transport, method, host, path, query, req_headers, req_body, status,
        resp_headers, resp_body, ms, error"""
        if self.ignored(rec.get("host", "")):
            return
        kind = self.kind(rec["method"], rec.get("host", ""), rec.get("path", ""))
        if self._auth_re.search(rec.get("path", "") or ""):
            rec = dict(rec, req_body="<redacted: credential exchange>", resp_body="<redacted: credential exchange>")
            kind = "read"
        q = rec.get("query")
        if q:
            rec = dict(rec, query={k: ("<redacted>" if self._scrub_re.match(f"{k}=x") else v) for k, v in q.items()})
        req_lim = self.cfg["write_request_max"] if kind == "write" else self.cfg["read_request_max"]
        resp_lim = self.cfg["write_response_max"] if kind == "write" else self.cfg["read_response_max"]
        ts = store.now_iso()
        rid = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        out = {
            "id": rid, "ts": ts, "run": self.run, "why": self.why, "kind": kind,
            "transport": rec["transport"], "method": rec["method"], "host": rec.get("host"),
            "path": rec.get("path"), "query": rec.get("query") or None,
            "request": {"headers": self.redact_headers(rec.get("req_headers")),
                        "body": self.body(rec.get("req_body"), req_lim)},
            "status": rec.get("status"),
            "response": {"body": self.body(rec.get("resp_body"), resp_lim)},
            "ms": rec.get("ms"), "error": rec.get("error"),
            "cwd": self.cwd, "argv": self.argv,
        }
        day = ts[:10]
        line = json.dumps(out, ensure_ascii=False, default=str)
        with _lock:
            (self.dir / "calls").mkdir(parents=True, exist_ok=True)
            with open(self.dir / "calls" / f"{day}.jsonl", "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if kind == "write":
                self.writes += 1
                status = out["status"] if out["status"] is not None else (out["error"] or "?")
                why = f" · why: {self.why}" if self.why else ""
                with open(self.dir / "CHANGES.md", "a", encoding="utf-8") as f:
                    f.write(f"- {ts[:16].replace('T', ' ')} · {out['host']} · {out['method']} {out['path']} · {status}{why} · `{rid}`\n")
            else:
                self.reads += 1

    def finish(self, exit_code=None, started=None):
        rec = {"run": self.run, "argv": self.argv, "cwd": self.cwd, "why": self.why,
               "started": started, "ended": store.now_iso(), "reads": self.reads,
               "writes": self.writes, "exit": exit_code}
        with _lock:
            (self.dir / "runs").mkdir(parents=True, exist_ok=True)
            with open(self.dir / "runs" / f"{rec['ended'][:10]}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec


# ---- hooks -----------------------------------------------------------------------------------
def _split(url):
    u = urlsplit(url)
    return u.hostname or "", u.path or "/", dict(parse_qsl(u.query)) if u.query else None

def _hook_requests(rec):
    try:
        import requests
    except ImportError:
        return False
    orig = requests.Session.send
    if getattr(orig, "_docket", False):
        return True
    def send(self, request, **kw):
        t0 = time.time(); resp = None; err = None
        try:
            resp = orig(self, request, **kw)
            return resp
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:500]
            raise
        finally:
            try:
                host, path, q = _split(request.url)
                rec.record({"transport": "http", "method": request.method.upper(), "host": host,
                            "path": path, "query": q, "req_headers": dict(request.headers),
                            "req_body": request.body,
                            "status": resp.status_code if resp is not None else None,
                            "resp_body": (resp.content if resp is not None and not kw.get("stream") else None),
                            "ms": int((time.time() - t0) * 1000), "error": err})
            except Exception as e:  # never break the caller
                sys.stderr.write(f"[docket] record failed: {e}\n")
    send._docket = True
    requests.Session.send = send
    return True

def _hook_httpx(rec):
    if "httpx" not in sys.modules:
        try:
            import importlib.util
            if importlib.util.find_spec("httpx") is None:
                return False
        except Exception:
            return False
    import httpx
    def wrap(cls, is_async):
        orig = cls.send
        if getattr(orig, "_docket", False):
            return
        def after(request, resp, err, t0):
            try:
                host, path, q = _split(str(request.url))
                body = None
                if resp is not None:
                    try:
                        body = resp.content
                    except Exception:
                        body = None
                rec.record({"transport": "http", "method": request.method.upper(), "host": host,
                            "path": path, "query": q, "req_headers": dict(request.headers),
                            "req_body": request.content, "status": resp.status_code if resp is not None else None,
                            "resp_body": body, "ms": int((time.time() - t0) * 1000), "error": err})
            except Exception as e:
                sys.stderr.write(f"[docket] record failed: {e}\n")
        if is_async:
            async def send(self, request, **kw):
                t0 = time.time(); resp = None; err = None
                try:
                    resp = await orig(self, request, **kw)
                    if not kw.get("stream"):
                        await resp.aread()
                    return resp
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"[:500]; raise
                finally:
                    after(request, resp, err, t0)
        else:
            def send(self, request, **kw):
                t0 = time.time(); resp = None; err = None
                try:
                    resp = orig(self, request, **kw)
                    if not kw.get("stream"):
                        resp.read()
                    return resp
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"[:500]; raise
                finally:
                    after(request, resp, err, t0)
        send._docket = True
        cls.send = send
    wrap(httpx.Client, False)
    wrap(httpx.AsyncClient, True)
    return True

def _hook_urllib(rec):
    import urllib.request as ur
    orig = ur.OpenerDirector.open
    if getattr(orig, "_docket", False):
        return True
    def open_(self, fullurl, data=None, timeout=None):
        req = fullurl if isinstance(fullurl, ur.Request) else ur.Request(fullurl, data=data)
        t0 = time.time(); resp = None; err = None; status = None; body = None
        try:
            resp = orig(self, fullurl, data, timeout) if timeout is not None else orig(self, fullurl, data)
            status = getattr(resp, "status", None)
            # read once, then hand the caller a replay of the same bytes
            body = resp.read()
            import io
            replay = io.BytesIO(body)
            for attr in ("status", "headers", "url", "code", "msg", "reason"):
                if hasattr(resp, attr):
                    try: setattr(replay, attr, getattr(resp, attr))
                    except Exception: pass
            replay.getcode = lambda: status
            replay.info = lambda: getattr(resp, "headers", None)
            replay.geturl = lambda: getattr(resp, "url", req.full_url)
            return replay
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:500]
            status = getattr(e, "code", None)
            raise
        finally:
            try:
                host, path, q = _split(req.full_url)
                rec.record({"transport": "http", "method": req.get_method().upper(), "host": host,
                            "path": path, "query": q, "req_headers": dict(req.header_items()),
                            "req_body": req.data, "status": status, "resp_body": body,
                            "ms": int((time.time() - t0) * 1000), "error": err})
            except Exception as e:
                sys.stderr.write(f"[docket] record failed: {e}\n")
    open_._docket = True
    ur.OpenerDirector.open = open_
    return True

def _proto_to_obj(msg):
    try:
        from google.protobuf.json_format import MessageToDict
        from google.protobuf.message import Message
        if isinstance(msg, Message):
            return MessageToDict(msg, preserving_proto_field_name=True)
        # proto-plus wrapper (google-ads)
        if hasattr(type(msg), "pb"):
            return MessageToDict(type(msg).pb(msg), preserving_proto_field_name=True)
    except Exception:
        pass
    return str(msg)[:20000]

def _hook_grpc(rec):
    try:
        import importlib.util
        if importlib.util.find_spec("grpc") is None:
            return False
        import grpc
    except Exception:
        return False
    if getattr(grpc, "_docket", False):
        return True

    class Trail(grpc.UnaryUnaryClientInterceptor, grpc.UnaryStreamClientInterceptor):
        def __init__(self, target):
            self.host = target.split(":")[0]
        def _meta(self, details):
            return {k: v for k, v in (details.metadata or [])}
        def intercept_unary_unary(self, continuation, details, request):
            t0 = time.time(); err = None; status = None; resp_obj = None
            call = continuation(details, request)
            try:
                resp_obj = _proto_to_obj(call.result())
                status = "OK"
            except Exception as e:
                err = f"{type(e).__name__}: {e}"[:2000]
                try: status = call.code().name
                except Exception: status = "ERROR"
            self._rec(details, request, status, resp_obj, err, t0)
            return call
        def intercept_unary_stream(self, continuation, details, request):
            t0 = time.time()
            call = continuation(details, request)
            self._rec(details, request, "STREAM", None, None, t0)
            return call
        def _rec(self, details, request, status, resp_obj, err, t0):
            try:
                meta = self._meta(details)
                rec.record({"transport": "grpc", "method": "RPC", "host": self.host,
                            "path": details.method, "query": None, "req_headers": meta,
                            "req_body": json.dumps(_proto_to_obj(request), default=str),
                            "status": status,
                            "resp_body": json.dumps(resp_obj, default=str) if resp_obj is not None else None,
                            "ms": int((time.time() - t0) * 1000), "error": err})
            except Exception as e:
                sys.stderr.write(f"[docket] record failed: {e}\n")

    orig_secure, orig_insecure = grpc.secure_channel, grpc.insecure_channel
    def secure_channel(target, *a, **kw):
        return grpc.intercept_channel(orig_secure(target, *a, **kw), Trail(target))
    def insecure_channel(target, *a, **kw):
        return grpc.intercept_channel(orig_insecure(target, *a, **kw), Trail(target))
    grpc.secure_channel, grpc.insecure_channel = secure_channel, insecure_channel
    grpc._docket = True
    # gRPC method names: treat Mutate/Create/Update/Remove/Upload as writes, everything else read
    rec.cfg.setdefault("rules", []).insert(0, {"host": "*", "path_regex": r"/(Mutate|Create|Update|Remove|Delete|Upload|Apply|Set)\w*$", "kind": "write"})
    rec.cfg["rules"].append({"host": "*", "path_regex": r"^/[\w.]+/(Search|Get|List|Generate|Suggest|Query)\w*$", "kind": "read"})
    return True

def install(trail_dir=None, why=None):
    """Attach the hooks in this process. Safe to call twice. Returns the Recorder."""
    global _installed
    if _installed:
        return _installed
    d = store.find_dir(trail_dir)
    if d is None:
        raise RuntimeError("docket: no trail directory. Run `docket init` or set DOCKET_DIR.")
    rec = Recorder(d, why=why)
    _hook_requests(rec); _hook_httpx(rec); _hook_urllib(rec); _hook_grpc(rec)
    # libraries imported later: hook them on import
    import builtins
    orig_import = builtins.__import__
    def imp(name, *a, **kw):
        m = orig_import(name, *a, **kw)
        try:
            if name.split(".")[0] in ("requests", "httpx", "grpc"):
                {"requests": _hook_requests, "httpx": _hook_httpx, "grpc": _hook_grpc}[name.split(".")[0]](rec)
        except Exception:
            pass
        return m
    builtins.__import__ = imp
    started = store.now_iso()
    import atexit
    atexit.register(lambda: rec.finish(started=started))
    _installed = rec
    return rec
