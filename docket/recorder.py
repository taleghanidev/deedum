"""The in-process hooks. `install()` patches requests, httpx, urllib and gRPC so every call the
process makes is appended to the trail. No third-party imports at module level — the hooks attach
lazily, only if the library is already importable."""
import os, sys, json, time, uuid, fnmatch, re, threading, pathlib, datetime
from urllib.parse import urlsplit, parse_qsl
from . import store

_lock = threading.Lock()
_installed = False


# ---- last line of defence: scrub key-shaped VALUES from the serialized record ---------------
# Field-name redaction misses keys that travel under an unexpected name, inside a URL, a text
# body, a response or argv. Everything written to the trail passes through scrub_values() first.
_VALUE_RES = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"(?i)\b(bearer|basic)(\s+|%20)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?<=://)[^/\s:@\"']+:[^/\s@\"']+(?=@)"),                  # user:pass@ in URLs
    re.compile(r"\b(sk|rk|pk_live|sk_live|sk_test|rk_live|whsec|xai|gsk|pplx|fc)[-_][A-Za-z0-9_-]{20,}"),
    re.compile(r"\bsk-(ant|or|proj|svcacct|admin)?-?[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
    re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}"),
    re.compile(r"\b1//0[0-9A-Za-z_-]{30,}"),
    re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bEAA[A-Za-z0-9]{30,}"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bre_[A-Za-z0-9]{8}_[A-Za-z0-9]{16,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),   # JWT
    re.compile(r"\b[a-z]{2,6}_[A-Za-z0-9]{32,}\b"),                       # prefix_longrandom (cfk_, cfut_, ...)
]
# `x-access-token: abc`, `rwg_token=abc`, `X-Auth-Key: abc` inside argv, text bodies and URLs.
_NAMEVAL_RE = re.compile(r"(?i)(?<!<)\b([A-Za-z0-9_-]*(?:token|secret|passw|credential|auth[-_]?key|api[-_]?key|access[-_]?key|private[-_]?key|client[-_]?key|websocket-key)[A-Za-z0-9_-]*\s*[:=]\s*)(?!<redacted>|<secret:)[^\s\"'&<>,;\\]{6,}")
_SECRET_ENV_RE = re.compile(r"(?i)(key|token|secret|passw|pwd|credential|auth|private|dsn|cookie|session)")
_known = {"stamp": None, "values": []}


def _env_files():
    d, out = pathlib.Path.cwd().resolve(), []
    home = pathlib.Path.home().resolve()
    for p in [d, *d.parents]:
        out += [f for f in p.glob(".env*") if f.is_file() and not f.name.endswith((".example", ".sample", ".template"))]
        if p == home:
            break
    return out


def _secret_values():
    """(value, NAME) for every secret-named env var and .env entry, longest first. Wherever a value
    appears it is replaced by <secret:NAME#fp>, so the trail says WHICH credential a call used
    (and, through the fingerprint, which version of it) without ever holding the value."""
    files = _env_files()
    stamp = (tuple(sorted(os.environ.items())), tuple((str(f), f.stat().st_mtime) for f in files))
    if stamp == _known["stamp"]:
        return _known["values"]
    pairs = list(os.environ.items())
    for f in files:
        try:
            for ln in f.read_text(errors="ignore").splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, v = ln.removeprefix("export ").split("=", 1)
                    pairs.append((k.strip(), v.strip().strip("'\"")))
        except OSError:
            pass
    named = {}
    for k, v in pairs:
        k = k.strip()
        if _SECRET_ENV_RE.search(k) and len(v) >= 12 and not v.startswith(("/", "http")) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            named.setdefault(v, k)  # the process env wins over .env files for the name
    _known.update(stamp=stamp, values=sorted(named.items(), key=lambda kv: len(kv[0]), reverse=True))
    return _known["values"]


def secrets_in(*texts):
    """Names of the known secrets that appear in any of these strings (before redaction)."""
    found = []
    blob = "\n".join(t for t in texts if isinstance(t, str))
    if not blob:
        return found
    for v, name in _secret_values():
        if v in blob and name not in found:
            found.append(name)
    return found


def scrub_values(text):
    for v, name in _secret_values():
        if v in text:
            tag = store.secret_tag(name, v)
            text = text.replace(v, tag)
            esc = json.dumps(v)[1:-1]
            if esc != v:
                text = text.replace(esc, tag)
    for r in _VALUE_RES:
        text = r.sub("<redacted>", text)
    return _NAMEVAL_RE.sub(lambda m: m.group(1) + "<redacted>", text)


class DocketBlocked(PermissionError):
    """Raised instead of sending a write that `docket run --dry` or require_why_hosts refused."""


class Recorder:
    def __init__(self, trail_dir, why=None, run_id=None):
        self.dir = pathlib.Path(trail_dir)
        self.cfg = store.load_config(self.dir)
        self._why_arg = why or os.environ.get("DOCKET_WHY") or ""
        self.dry = os.environ.get("DOCKET_DRY") == "1"
        env_run = os.environ.get("DOCKET_RUN")
        if env_run == "always":  # always-on mode: group by the Claude Code session when there is one
            env_run = ("session-" + os.environ["CLAUDE_CODE_SESSION_ID"][:8]) if os.environ.get("CLAUDE_CODE_SESSION_ID") else None
        self.run = run_id or env_run or uuid.uuid4().hex[:8]
        self.cwd = os.getcwd()
        self.project = store.project_of(self.cwd)
        self.reads = 0
        self.writes = 0
        self._redact_h = {h.lower() for h in self.cfg["redact_headers"]}
        self._redact_b = {k.lower() for k in self.cfg["redact_body_keys"]}
        self._read_re = re.compile(self.cfg["read_path_regex"])
        self._auth_re = re.compile(self.cfg.get("auth_path_regex") or r"$^")
        self._scrub_re = re.compile(self.cfg.get("scrub_regex") or r"$^")
        self.argv = self.scrub_argv(" ".join(sys.argv))[:2000]

    @property
    def why(self):
        """--why / DOCKET_WHY, else the session's reason set with `docket why "..."` (re-read on
        every call, so a reason set mid-session applies to the very next call)."""
        return self._why_arg or store.get_session_why(self.dir) or ""

    def gate(self, method, host, path, body=None):
        """Called before a request leaves. Returns a refusal reason, or None to let it go."""
        kind = self.kind(method, host or "", path or "")
        gql = self._graphql_kind(body)
        if gql:
            kind = gql
        if kind != "write" or self._auth_re.search(path or ""):
            return None
        if self.dry:
            return "docket dry run: write not sent"
        if not self.why and any(fnmatch.fnmatch((host or "").lower(), p) for p in self.cfg.get("require_why_hosts", [])):
            return f"docket: writes to {host} need a reason (docket run --why, or docket why \"...\")"
        return None

    def blocked(self, transport, method, host, path, query, headers, body, reason):
        self.record({"transport": transport, "method": method, "host": host, "path": path, "query": query,
                     "req_headers": headers, "req_body": body, "status": "BLOCKED", "resp_body": None,
                     "ms": 0, "error": reason})
        return DocketBlocked(reason)

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

    _gql_re = re.compile(r"^\s*(mutation|query|subscription)\b", re.I)

    def _graphql_kind(self, raw):
        """GraphQL sends reads and writes to one URL; look at the operation instead."""
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", "replace")
            if not isinstance(raw, str) or '"query"' not in raw[:200]:
                return None
            q = json.loads(raw).get("query", "")
            m = self._gql_re.match(q)
            if m:
                return "write" if m.group(1).lower() == "mutation" else "read"
            return "read" if q.lstrip().startswith("{") else None
        except Exception:
            return None

    def redact_headers(self, headers):
        out = {}
        for k, v in (headers or {}).items():
            v = v.decode("latin-1") if isinstance(v, bytes) else str(v)
            if k.lower() in self._redact_h or self._secret_name_re.search(k):
                named = secrets_in(v)
                # keep the scheme ("Bearer") and say which credential it was
                scheme = v.split(" ", 1)[0] + " " if " " in v and len(v.split(" ", 1)[0]) < 12 else ""
                out[k] = scheme + ("".join(store.secret_tag(n, sv) for sv, n in _secret_values() if n in named[:1]) if named else "<redacted>")
            else:
                out[k] = v[:500]
        return out

    def redact_obj(self, obj, depth=0):
        if depth > 30:
            return obj
        if isinstance(obj, dict):
            return {k: ("<redacted>" if str(k).lower() in self._redact_b or self._secret_name_re.search(str(k)) else self.redact_obj(v, depth + 1))
                    for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.redact_obj(v, depth + 1) for v in obj]
        return obj

    # Any header or body field whose NAME looks secret is redacted, not just the exact names in
    # config: X-Auth-Key, xi-api-key, accessToken, input_token, appsecret_proof all slipped past the lists.
    _secret_name_re = re.compile(r"(?i)(token|secret|passw|auth|credential|proof|signature|cookie|session|key$)")

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
        host = rec.get("host", "") or ""
        if not store.valid_host(host) or self.ignored(host):
            return
        kind = self.kind(rec["method"], host, rec.get("path", ""))
        gql = self._graphql_kind(rec.get("req_body"))
        if gql:
            kind = gql
        if kind == "read" and any(fnmatch.fnmatch(host.lower(), p) for p in self.cfg.get("ignore_read_hosts", [])):
            return
        raw_body = rec.get("req_body")
        used = secrets_in(json.dumps({k: (v.decode("latin-1") if isinstance(v, bytes) else str(v)) for k, v in (rec.get("req_headers") or {}).items()}),
                          raw_body.decode("utf-8", "replace") if isinstance(raw_body, (bytes, bytearray)) else raw_body if isinstance(raw_body, str) else None,
                          json.dumps(rec.get("query") or {}), self.argv)
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
        why = self.why
        out = {
            "id": rid, "ts": ts, "run": self.run, "why": why, "kind": kind,
            "project": self.project["name"], "folder": self.project["folder"], "branch": self.project["branch"],
            "service": store.service_of(host, self.cfg), "secrets": used or None,
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
        line = scrub_values(json.dumps(out, ensure_ascii=False, default=str))
        with _lock:
            (self.dir / "calls").mkdir(parents=True, exist_ok=True)
            with open(self.dir / "calls" / f"{day}.jsonl", "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if kind == "write":
                self.writes += 1
                status = out["status"] if out["status"] is not None else (out["error"] or "?")
                whys = f" · why: {why}" if why else ""
                proj = f"[{out['project']}] " if out["project"] else ""
                with open(self.dir / "CHANGES.md", "a", encoding="utf-8") as f:
                    f.write(scrub_values(f"- {ts[:16].replace('T', ' ')} · {proj}{out['service']} · {out['method']} {out['host']}{out['path']} · {status}{whys} · `{rid}`\n"))
            else:
                self.reads += 1

    def finish(self, exit_code=None, started=None):
        if self.reads + self.writes == 0 and os.environ.get("DOCKET_RUN") in (None, "always"):
            return None  # a process that made no calls leaves no run line (always-on mode would flood runs/)
        rec = {"run": self.run, "argv": self.argv, "cwd": self.cwd, "why": self.why,
               "project": self.project["name"], "folder": self.project["folder"], "branch": self.project["branch"],
               "started": started, "ended": store.now_iso(), "reads": self.reads,
               "writes": self.writes, "exit": exit_code}
        with _lock:
            (self.dir / "runs").mkdir(parents=True, exist_ok=True)
            with open(self.dir / "runs" / f"{rec['ended'][:10]}.jsonl", "a", encoding="utf-8") as f:
                f.write(scrub_values(json.dumps(rec, ensure_ascii=False)) + "\n")
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
        host, path, q = _split(request.url)
        why_not = rec.gate(request.method.upper(), host, path, request.body)
        if why_not:
            raise rec.blocked("http", request.method.upper(), host, path, q, dict(request.headers), request.body, why_not)
        t0 = time.time(); resp = None; err = None
        _tl.inner = getattr(_tl, "inner", 0) + 1
        try:
            resp = orig(self, request, **kw)
            return resp
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:500]
            raise
        finally:
            _tl.inner -= 1
            try:
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
        def check(request):
            host, path, q = _split(str(request.url))
            why_not = rec.gate(request.method.upper(), host, path, request.content)
            if why_not:
                raise rec.blocked("http", request.method.upper(), host, path, q, dict(request.headers), request.content, why_not)
        if is_async:
            async def send(self, request, **kw):
                check(request)
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
                check(request)
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
        h0, p0, q0 = _split(req.full_url)
        why_not = rec.gate(req.get_method().upper(), h0, p0, data if data is not None else req.data)
        if why_not:
            raise rec.blocked("http", req.get_method().upper(), h0, p0, q0, dict(req.header_items()), data if data is not None else req.data, why_not)
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

_tl = threading.local()


def _hook_urllib3(rec):
    """botocore (AWS SDK), pip and many SDKs talk to urllib3 directly, not through requests.
    Calls made BY requests also pass through here; those are skipped (requests recorded them)."""
    try:
        import importlib.util
        if "urllib3" not in sys.modules and importlib.util.find_spec("urllib3") is None:
            return False
        import urllib3.connectionpool as cp
    except Exception:
        return False
    orig = cp.HTTPConnectionPool.urlopen
    if getattr(orig, "_docket", False):
        return True
    def urlopen(self, method, url, body=None, headers=None, *a, **kw):
        if getattr(_tl, "inner", 0) > 0 or getattr(_tl, "u3", 0) > 0:
            return orig(self, method, url, body, headers, *a, **kw)
        host = self.host
        u = urlsplit(url) if "://" in url else None
        path = (u.path if u else url.split("?", 1)[0]) or "/"
        query = dict(parse_qsl(u.query if u else (url.split("?", 1)[1] if "?" in url else ""))) or None
        why_not = rec.gate(method.upper(), host, path, body)
        if why_not:
            raise rec.blocked("http", method.upper(), host, path, query, dict(headers or {}), body, why_not)
        t0 = time.time(); resp = None; err = None
        _tl.u3 = 1  # retries/redirects re-enter urlopen: record the outer call once
        try:
            resp = orig(self, method, url, body, headers, *a, **kw)
            return resp
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:500]; raise
        finally:
            _tl.u3 = 0
            try:
                data = None
                if resp is not None and kw.get("preload_content", True):
                    data = getattr(resp, "data", None)
                rec.record({"transport": "http", "method": method.upper(), "host": host, "path": path,
                            "query": query, "req_headers": dict(headers or {}),
                            "req_body": body if isinstance(body, (bytes, str)) else None,
                            "status": getattr(resp, "status", None), "resp_body": data,
                            "ms": int((time.time() - t0) * 1000), "error": err})
            except Exception as e:
                sys.stderr.write(f"[docket] record failed: {e}\n")
    urlopen._docket = True
    cp.HTTPConnectionPool.urlopen = urlopen
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
    _hook_requests(rec); _hook_httpx(rec); _hook_urllib(rec); _hook_grpc(rec); _hook_urllib3(rec)
    # libraries imported later: hook them on import
    import builtins
    orig_import = builtins.__import__
    def imp(name, *a, **kw):
        m = orig_import(name, *a, **kw)
        try:
            top = name.split(".")[0]
            if top in ("requests", "httpx", "grpc", "urllib3", "botocore"):
                {"requests": _hook_requests, "httpx": _hook_httpx, "grpc": _hook_grpc,
                 "urllib3": _hook_urllib3, "botocore": _hook_urllib3}[top](rec)
        except Exception:
            pass
        return m
    builtins.__import__ = imp
    started = store.now_iso()
    import atexit
    atexit.register(lambda: rec.finish(started=started))
    _installed = rec
    return rec
