"""The in-process hooks. `install()` patches requests, httpx, urllib and gRPC so every call the
process makes is appended to the trail. No third-party imports at module level — the hooks attach
lazily, only if the library is already importable."""
import os, sys, json, time, uuid, fnmatch, re, threading, pathlib, datetime, shlex
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
    re.compile(r"(?<!\d)\d{8,12}:AA[A-Za-z0-9_-]{30,}"),                   # Telegram bot token (follows "bot" in URLs)
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


# A reason is typed by a person ("log in with password hunter2 and change the plan"): the value
# is not key-shaped, so scrub_values() alone keeps it. The word after a credential noun goes.
_PROSE_SECRET_RE = re.compile(r"(?i)\b(password|passwd|passphrase|pin|otp|secret|token|api[ -]?key|access[ -]?key|private[ -]?key)"
                              r"((?:\s+(?:is|was|of|for|=|:|to))?\s*)(?:\"[^\"]{1,200}\"|'[^']{1,200}'|`[^`]{1,200}`|[^\s\"'`,;]{4,})")


def scrub_prose(text):
    """What `--why` and the Claude prompt hook store: value scrubbing plus 'password X' prose."""
    return _PROSE_SECRET_RE.sub(lambda m: m.group(1) + m.group(2) + "<redacted>", scrub_values(text or ""))


# Which header or body field NAMES are credentials. Split the name into words (snake, kebab,
# camel) and look at the last one: `access_token`, `X-Auth-Key`, `appsecret_proof`, `accessToken`
# and `developertoken` are secrets; `max_tokens`, `author`, `keyword`, `session_name` and
# `monkey` are not. Until 0.3 a substring match redacted all of the latter.
_LAST_WORD_SECRET = {"token", "secret", "password", "passwd", "passphrase", "pwd", "auth", "authorization", "authentication",
                     "credential", "credentials", "proof", "signature", "sig", "cookie", "cookies", "key",
                     "apikey", "session", "sessionid", "sid", "bearer", "jwt", "otp", "pin", "totp", "mfa",
                     "verifier", "challenge", "assertion", "nonce", "csrf", "xsrf"}   # oauth_verifier, code_challenge, client_assertion
_PLURAL_QUALIFIERS = {"access", "refresh", "api", "auth", "oauth", "secret", "session", "id", "bearer", "private", "public", "client",
                      "service", "personal", "deploy", "webhook", "signing", "encryption", "master", "app", "user", "account",
                      "ssh", "gpg", "pgp", "recovery", "backup", "temporary", "temp", "long", "short", "lived", "static", "dynamic"}
_VALUE_WORDS = {"value", "values", "string", "str", "data", "hash", "ticket", "blob", "bytes", "b64", "base64", "hex", "raw", "text", "encrypted", "enc", "plain", "plaintext"}
_CODE_OWNERS = {"authorization", "auth", "oauth", "verification", "confirmation", "activation", "recovery", "reset", "access", "security", "mfa", "otp", "backup"}
_WORD_SPLIT_RE = re.compile(r"[_\-\s.]+|(?<=[a-z0-9])(?=[A-Z])")


def secret_name(name):
    words = [w.lower() for w in _WORD_SPLIT_RE.split(str(name)) if w]
    while words and re.fullmatch(r"v?\d+", words[-1]):   # token_v2, secret_1, key2024: the version is not the word
        words.pop()
    if not words:
        return False
    last = re.sub(r"\d+$", "", words[-1]) or words[-1]        # secret1, token2
    if last in _LAST_WORD_SECRET or last.endswith(("token", "secret", "password", "passwd", "sessid", "sessionid")):
        return True
    if last == "id" and len(words) >= 2 and words[-2] in ("session", "auth", "sess"):   # session_id, sessionId
        return True
    if last == "code" and len(words) >= 2 and words[-2] in _CODE_OWNERS:   # authorization_code, verification_code
        return True
    # plurals: access_tokens, api_keys, client_secrets, a bare "secrets"/"credentials"; not
    # max_tokens, input_tokens, total_tokens (a count: the word before is not a credential qualifier)
    if last.endswith("s") and (last[:-1] in _LAST_WORD_SECRET or last[:-1].endswith(("token", "secret", "password"))):
        if len(words) == 1 and last[:-1] != "token":
            return True
        if len(words) >= 2 and words[-2] in _PLURAL_QUALIFIERS:
            return True
    # tokenValue, auth_token_value, session_ticket, secret_data, password_hash: a credential word
    # followed by a word that still names the value itself (not a count, name, type or policy)
    if last in _VALUE_WORDS and len(words) >= 2 and (words[-2] in _LAST_WORD_SECRET or words[-2].endswith(("token", "secret", "password", "passwd"))):
        return True
    # `password_confirm`, `token_v2`, `secret1`: a credential word anywhere, followed only by a qualifier
    return any(w in ("password", "passwd", "secret") for w in words[:-1]) and len(words) <= 3


_SECRET_FLAG = re.compile(r"(?i)^--?[a-z0-9-]*(secret|token|passw|password|passphrase|credential|api-?key|access-?key|private-?key|"
                          r"client-?key|auth-?key|session-?key|signing-?key|master-?key|pin|otp)[a-z0-9-]*$")


_SECRET_FLAG_IN = re.compile(r"(?i)(^|\s)--?[a-z0-9-]*(secret|token|passw|password|passphrase|credential|api-?key|access-?key|private-?key|"
                             r"client-?key|auth-?key|session-?key|signing-?key|master-?key|pin|otp)[a-z0-9-]*(=|\s)")


# Binaries whose `-p` / `-pVALUE` is a password, not a port or a path.
_P_IS_PASSWORD = {"az", "sshpass", "mysql", "mysqldump", "mysqladmin", "mariadb", "smbclient", "ftp", "lftp", "docker", "podman",
                  "mongo", "mongosh", "redis-cli", "cqlsh", "sqlcmd", "wget", "htpasswd", "openconnect", "rclone", "curl"}


def scrub_argv_list(argv, depth=0, quote=True):
    """Redact the WHOLE value of every secret-named flag while argv is still a list (`--secret-string
    'correct horse battery staple'` keeps its spaces), then quote and join for the record."""
    out, i = [], 0
    exe = os.path.basename(argv[0]) if argv else ""
    p_is_pw = exe in _P_IS_PASSWORD or (exe.startswith("python") and False)
    while i < len(argv):
        a = argv[i]
        if a.startswith("-") and "=" in a and _SECRET_FLAG.match(a.split("=", 1)[0]):
            out.append(a.split("=", 1)[0] + "=<redacted>")
        elif _SECRET_FLAG.match(a) and i + 1 < len(argv):
            out += [a, "<redacted>"]; i += 1
        elif p_is_pw and a == "-p" and i + 1 < len(argv):
            out += [a, "<redacted>"]; i += 1
        elif p_is_pw and a.startswith("-p") and len(a) > 2 and not a.startswith("--"):
            out.append("-p<redacted>")
        elif any(c in a for c in " \t\n") and not a.startswith("-") and depth < 3 and _SECRET_FLAG_IN.search(a):
            # a command line inside an argument that carries a secret flag (ssh's remote command,
            # sh -c "deploy --password ..."): split it the way a shell will and scrub it as a list.
            # Arguments without one (Python source, JSON) are kept exactly as given; text that
            # cannot be parsed is omitted, not half-kept.
            try:
                out.append(scrub_argv_list(shlex.split(a), depth + 1, quote=False))
            except ValueError:
                out.append("<argument omitted: could not parse it for redaction>")
        else:
            out.append(a)
        i += 1
    if not quote:
        return " ".join(out)
    return " ".join(shlex.quote(x) if x != "<redacted>" and not x.startswith("<argument omitted") else x for x in out)


# An operation is the keyword, an optional name, then its selection set or variables: `query A {`,
# `mutation (`, `subscription @dir {`. Prose that starts with the word ("subscription cancel
# please") is not GraphQL and must not pass as a read.
_GQL_OPS = re.compile(r"(?:^|[}\s])(mutation|query|subscription)\b\s*([A-Za-z_][A-Za-z0-9_]*)?\s*(?=[({@])", re.I)


def graphql_kind(q, operation_name=None):
    """A document can hold several operations; `operationName` picks one. The chosen one decides;
    without a name, any mutation in the document makes it a write (a server runs the only
    operation, and a document with several and no name is an error we treat as a write)."""
    if not isinstance(q, str):
        return None
    ops = [(m.group(1).lower(), m.group(2)) for m in _GQL_OPS.finditer(q)]
    if operation_name:
        for kind, name in ops:
            if name == operation_name:
                return "write" if kind == "mutation" else "read"
        return "write" if ops else None   # a name that matches nothing: not provably a read
    if ops:
        return "write" if any(k == "mutation" for k, _ in ops) else "read"
    return "read" if q.lstrip().startswith("{") else None


class DeedumBlocked(PermissionError):
    """Raised instead of sending a write that `deedum run --dry` or require_why_hosts refused."""


class Recorder:
    def __init__(self, trail_dir, why=None, run_id=None):
        self.dir = pathlib.Path(trail_dir)
        self.cfg = store.load_config(self.dir)
        self._why_arg = scrub_prose(why or os.environ.get("DEEDUM_WHY") or "")
        self.dry = os.environ.get("DEEDUM_DRY") == "1"
        env_run = os.environ.get("DEEDUM_RUN")
        if env_run == "always":  # always-on mode: group by the Claude Code session when there is one
            env_run = ("session-" + os.environ["CLAUDE_CODE_SESSION_ID"][:8]) if os.environ.get("CLAUDE_CODE_SESSION_ID") else None
        self.run = run_id or env_run or uuid.uuid4().hex[:8]
        self.cwd = os.getcwd()
        self.project = store.project_of(self.cwd)
        self.agent = store.agent_context(self.dir)
        self.reads = 0
        self.writes = 0
        self._redact_h = {h.lower() for h in self.cfg["redact_headers"]}
        self._redact_b = {k.lower() for k in self.cfg["redact_body_keys"]}
        self._read_re = re.compile(self.cfg["read_path_regex"])
        self._auth_re = re.compile(self.cfg.get("auth_path_regex") or r"$^")
        self._scrub_re = re.compile(self.cfg.get("scrub_regex") or r"$^")
        self.argv = self.scrub_argv(scrub_argv_list(sys.argv))[:2000]   # list-aware first: a multiword secret stays whole

    @property
    def why(self):
        """--why / DEEDUM_WHY, else the session's reason set with `deedum why "..."` (re-read on
        every call, so a reason set mid-session applies to the very next call)."""
        return self._why_arg or store.get_session_why(self.dir) or ""

    def gate(self, method, host, path, body=None, transport="http"):
        """Called before a request leaves. Returns a refusal reason, or None to let it go."""
        kind = self.kind(method, host or "", path or "")
        gql = self._graphql_kind(body)
        if gql:
            kind = gql
        # A dry run blocks every request that is not read-only BY METHOD, whatever the path
        # says: read_path_regex is a labelling heuristic (POST /v1/tokens, POST /oauth/clients
        # and POST /webhooks/status all went out under --dry until 0.3), and the gate must not
        # depend on it. A GraphQL query is the one POST that is provably a read.
        if self.dry and method.upper() not in ("GET", "HEAD", "OPTIONS") and gql != "read" and not (transport == "grpc" and kind == "read"):
            return "deedum dry run: write not sent"
        if kind != "write" or (transport == "http" and self._auth_re.search(path or "")):
            return None
        if not self.why and any(fnmatch.fnmatch((host or "").lower(), p) for p in self.cfg.get("require_why_hosts", [])):
            return f"deedum: writes to {host} need a reason (deedum run --why, or deedum why \"...\")"
        return self._policy(host, method, path, kind)

    def _policy(self, host, method, path, kind):
        """The policy file / config rules: refuse, hold for a person, or let go."""
        from . import policy as pol
        if pol.notifying():
            return None
        rules = pol.load(self.cfg, self.cwd)
        if not rules:
            return None
        verdict = pol.decide(rules, self.dir, host, method, kind, self.project["name"], self.why, self.run,
                             (self.agent or {}).get("session"))
        if isinstance(verdict, tuple) and verdict[0] == "approve":
            return pol.request_approval(self.dir, verdict[1], host, method, path, self.project["name"], self.why, self.agent,
                                        notify=rules.get("notify"))
        return verdict

    def blocked(self, transport, method, host, path, query, headers, body, reason, via=None):
        self.record({"transport": transport, "via": via, "method": method, "host": host, "path": path, "query": query,
                     "req_headers": headers, "req_body": body, "status": "BLOCKED", "resp_body": None,
                     "ms": 0, "error": reason})
        return DeedumBlocked(reason)

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
            doc = json.loads(raw)
            q = doc.get("query", "")
            return graphql_kind(q, doc.get("operationName"))
        except Exception:
            return None

    def redact_headers(self, headers):
        out = {}
        for k, v in (headers or {}).items():
            v = v.decode("latin-1") if isinstance(v, bytes) else str(v)
            if k.lower() in self._redact_h or secret_name(k):
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
            return {k: ("<redacted>" if str(k).lower() in self._redact_b or secret_name(k) else self.redact_obj(v, depth + 1))
                    for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.redact_obj(v, depth + 1) for v in obj]
        return obj

    # Any header or body field whose NAME is a credential word is redacted, not just the exact
    # names in config (secret_name above): X-Auth-Key, xi-api-key, accessToken, appsecret_proof.

    _bearer_re = re.compile(r"(?i)\b(bearer|basic|token|apikey|api-key)\s+[A-Za-z0-9._~+/=-]{6,}")
    _jsonkey_re = re.compile(r'(?i)("(?:[^"]*(?:token|secret|password|passwd|key|authorization|credential)[^"]*)"\s*:\s*)"[^"]*"')
    _hdr_re = re.compile(r"(?i)((?:authorization|developertoken|developer-token|x-api-key|api-key|apikey|cookie|x-auth-token|authenticationtoken)\s*:\s*)[^\s'\"]+(?:\s+[^\s'\"]+)?")

    _flag_re = re.compile(r"(?i)(\s(?:-u|--user|--proxy-user|--oauth2-bearer|--tlsuser|--tlspassword|--token|--password|--passwd|--secret|--api-key|--apikey|"
                          r"--client-secret|--secret-string|--secret-binary|--private-key|--access-key|--secret-access-key|--session-token|--auth-token|--bearer|--key-file|--sshpass)[= ])\S+")

    def scrub_argv(self, text):
        text = self._flag_re.sub(lambda m: m.group(1) + "<redacted>", text)
        text = self._hdr_re.sub(lambda m: m.group(1) + "<redacted>", text)
        text = self._bearer_re.sub(lambda m: m.group(1) + " <redacted>", text)
        text = self._jsonkey_re.sub(lambda m: m.group(1) + '"<redacted>"', text)
        return self.scrub(text)

    def scrub(self, text):
        return self._scrub_re.sub(lambda m: f"{m.group(1)}=<redacted>", text)

    _json_pair_re = re.compile(r'"([^"\\]{1,80})"\s*:\s*"(?:[^"\\]|\\.)*"')
    _json_open_re = re.compile(r'"([^"\\]{1,80})"\s*:\s*"(?:[^"\\]|\\.?)*$')   # a string cut by truncation (maybe on a backslash)
    _xml_el_re = re.compile(r"<((?:[A-Za-z_][\w.-]*:)?([A-Za-z_][\w.-]*))(\s[^>]*)?>([^<]{1,4000})</\1\s*>")   # <DeveloperToken>x</DeveloperToken>
    _xml_open_re = re.compile(r"<((?:[A-Za-z_][\w.-]*:)?([A-Za-z_][\w.-]*))(\s[^>]*)?>([^<]{1,4000})(</[^>]*)?$")   # cut inside, or inside its closing tag

    def redact_json_text(self, text):
        """Text that is JSON but could not be parsed (truncated, or embedded): every
        "name": "value" pair whose name is a credential word loses its value, including a
        value cut off by the truncation."""
        def pair(m):
            return f'"{m.group(1)}": "<redacted>"' if (m.group(1).lower() in self._redact_b or secret_name(m.group(1))) else m.group(0)

        def el(m):   # XML / SOAP: the element's local name decides, as a JSON key would
            return f"<{m.group(1)}{m.group(3) or ''}><redacted></{m.group(1)}>" if (m.group(2).lower() in self._redact_b or secret_name(m.group(2))) else m.group(0)
        text = self._json_pair_re.sub(pair, text)
        text = self._json_open_re.sub(pair, text)
        if "<" in text:
            text = self._xml_el_re.sub(el, text)
            text = self._xml_open_re.sub(lambda m: f"<{m.group(1)}{m.group(3) or ''}><redacted>" if (m.group(2).lower() in self._redact_b or secret_name(m.group(2))) else m.group(0), text)
        return text

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
                if size <= limit * 4 else {"_text": self.redact_json_text(raw[:limit]), "_bytes": size, "_truncated": True}
        except (ValueError, TypeError):
            pass
        if size > limit:
            return {"_text": self.redact_json_text(raw[:limit]), "_bytes": size, "_truncated": True}
        return {"_text": self.redact_json_text(raw)}

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
        # A credential exchange (an HTTP path: /oauth/token, /login) keeps no bodies and is not a
        # change. HTTP only: `aws iam delete-login-profile` is an exec write, whatever its words.
        if rec.get("transport") == "http" and self._auth_re.search(rec.get("path", "") or ""):
            rec = dict(rec, req_body="<redacted: credential exchange>", resp_body="<redacted: credential exchange>")
            kind = "read"
        q = rec.get("query")
        if q:
            rec = dict(rec, query={k: ("<redacted>" if self._scrub_re.search(f"{k}=x") or secret_name(k) else v) for k, v in q.items()})
        req_lim = self.cfg["write_request_max"] if kind == "write" else self.cfg["read_request_max"]
        resp_lim = self.cfg["write_response_max"] if kind == "write" else self.cfg["read_response_max"]
        ts = store.now_iso()
        rid = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        why = self.why
        out = {
            "id": rid, "ts": ts, "run": self.run, "why": why, "kind": kind,
            "project": self.project["name"], "folder": self.project["folder"], "branch": self.project["branch"],
            "agent": self.agent, "via": rec.get("via") or ("script" if rec["transport"] in ("http", "grpc") else rec["transport"]),
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
            store.append_line(self.dir / "calls" / f"{day}.jsonl", line)
            if kind == "write":
                self.writes += 1
                status = out["status"] if out["status"] is not None else (out["error"] or "?")
                whys = f" · why: {why}" if why else ""
                proj = f"[{out['project']}] " if out["project"] else ""
                store.append_line(self.dir / "CHANGES.md", scrub_values(
                    f"- {ts[:16].replace('T', ' ')} · {proj}{out['service']} · {out['method']} {out['host']}{out['path']} · {status}{whys} · `{rid}`"))
            else:
                self.reads += 1

    def finish(self, exit_code=None, started=None):
        for f in list(_pending):   # responses never read nor closed: recorded now, before the counts
            try:
                f()
            except Exception:
                pass
        if self.reads + self.writes == 0 and os.environ.get("DEEDUM_RUN") in (None, "always"):
            return None  # a process that made no calls leaves no run line (always-on mode would flood runs/)
        rec = {"run": self.run, "argv": self.argv, "cwd": self.cwd, "why": self.why,
               "project": self.project["name"], "folder": self.project["folder"], "branch": self.project["branch"],
               "agent": self.agent,
               "started": started, "ended": store.now_iso(), "reads": self.reads,
               "writes": self.writes, "exit": exit_code}
        with _lock:
            (self.dir / "runs").mkdir(parents=True, exist_ok=True)
            store.append_line(self.dir / "runs" / f"{rec['ended'][:10]}.jsonl", scrub_values(json.dumps(rec, ensure_ascii=False)))
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
    if getattr(orig, "_deedum", False):
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
                sys.stderr.write(f"[deedum] record failed: {e}\n")
    send._deedum = True
    requests.Session.send = send
    return True

def _httpx_content(request):
    """request.content raises RequestNotRead for files= and content=<iterator>: those are
    streams; the record says so instead of the hook breaking the upload."""
    try:
        return request.content
    except Exception:
        return None


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
        if getattr(orig, "_deedum", False):
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
                            "req_body": _httpx_content(request), "status": resp.status_code if resp is not None else None,
                            "resp_body": body, "ms": int((time.time() - t0) * 1000), "error": err})
            except Exception as e:
                sys.stderr.write(f"[deedum] record failed: {e}\n")
        def check(request):
            host, path, q = _split(str(request.url))
            why_not = rec.gate(request.method.upper(), host, path, _httpx_content(request))
            if why_not:
                raise rec.blocked("http", request.method.upper(), host, path, q, dict(request.headers), _httpx_content(request), why_not)
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
        send._deedum = True
        cls.send = send
    wrap(httpx.Client, False)
    wrap(httpx.AsyncClient, True)
    return True

_pending = set()   # flush callbacks of responses not yet recorded (urllib, http.client, aiohttp); Recorder.finish runs them


def _hook_urllib(rec):
    import urllib.request as ur
    orig = ur.OpenerDirector.open
    if getattr(orig, "_deedum", False):
        return True
    def open_(self, fullurl, data=None, timeout=None):
        req = fullurl if isinstance(fullurl, ur.Request) else ur.Request(fullurl, data=data)
        h0, p0, q0 = _split(req.full_url)
        why_not = rec.gate(req.get_method().upper(), h0, p0, data if data is not None else req.data)
        if why_not:
            raise rec.blocked("http", req.get_method().upper(), h0, p0, q0, dict(req.header_items()), data if data is not None else req.data, why_not)
        t0 = time.time()
        base = {"transport": "http", "method": req.get_method().upper(), "host": h0, "path": p0, "query": q0,
                "req_headers": dict(req.header_items()), "req_body": req.data}
        _tl.ul = getattr(_tl, "ul", 0) + 1  # the http.client hook underneath skips this call
        try:
            resp = orig(self, fullurl, data, timeout) if timeout is not None else orig(self, fullurl, data)
        except Exception as e:
            try:
                rec.record(dict(base, status=getattr(e, "code", None), resp_body=None, ms=int((time.time() - t0) * 1000),
                                error=f"{type(e).__name__}: {e}"[:500]))
            except Exception as e2:
                sys.stderr.write(f"[deedum] record failed: {e2}\n")
            raise
        finally:
            _tl.ul -= 1
        if not hasattr(resp, "isclosed"):
            return resp   # file: / data: / a custom opener: not a network call, nothing to record
        # The body is the caller's: a stream (server-sent events, a long poll, a download) is
        # read by them at their pace; the hook copies what passes, up to the budget, and writes
        # the record when the body ends or the response is closed.
        return _Tap(resp, rec, base, t0)
    open_._deedum = True
    ur.OpenerDirector.open = open_
    return True


class _Tap:
    """A response that records itself as it is consumed. Every attribute not listed here comes
    from the real response (getheader, headers, status, url, info, geturl ...)."""

    def __init__(self, resp, rec, base, t0):
        self._resp, self._rec, self._base, self._t0 = resp, rec, base, t0
        self._chunks, self._got, self._done = [], 0, False
        self._limit = max(rec.cfg["write_response_max"], rec.cfg["read_response_max"])
        _pending.add(self._flush)

    def _keep(self, data):
        if data and self._got < self._limit:
            part = bytes(data[: self._limit - self._got])
            self._chunks.append(part); self._got += len(part)

    def _flush(self):
        if self._done:
            return
        self._done = True
        _pending.discard(self._flush)
        try:
            self._rec.record(dict(self._base, status=getattr(self._resp, "status", None), resp_body=b"".join(self._chunks),
                                  ms=int((time.time() - self._t0) * 1000), error=None))
        except Exception as e:
            sys.stderr.write(f"[deedum] record failed: {e}\n")

    def read(self, amt=None):
        data = self._resp.read(amt)
        self._keep(data)
        if amt is None or (not data and amt != 0) or self._resp.isclosed():
            self._flush()
        return data

    def read1(self, n=-1):
        data = self._resp.read1(n)
        self._keep(data)
        if (not data and n != 0) or self._resp.isclosed():
            self._flush()
        return data

    def readinto(self, b):
        n = self._resp.readinto(b)
        self._keep(memoryview(b)[:n])
        if (not n and len(b)) or self._resp.isclosed():
            self._flush()
        return n

    def readline(self, limit=-1):
        data = self._resp.readline(limit)
        self._keep(data)
        if not data or self._resp.isclosed():
            self._flush()
        return data

    def readlines(self, hint=-1):
        lines = self._resp.readlines(hint)
        for ln in lines:
            self._keep(ln)
        self._flush()
        return lines

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def close(self):
        self._flush()
        return self._resp.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def getcode(self):
        return getattr(self._resp, "status", None)

    def __getattr__(self, name):
        return getattr(self._resp, name)


_tl = threading.local()


def _hook_http_client(rec):
    """The catch-all: http.client is under urllib, urllib3 (requests, botocore) and httplib2
    (google-api-python-client, which the Google Sheets / Search Console / YouTube clients use).
    Calls that an outer hook already records are skipped through the thread-local counters."""
    import http.client as hc
    if getattr(hc.HTTPConnection.request, "_deedum", False):
        return True
    orig_request, orig_getresponse = hc.HTTPConnection.request, hc.HTTPConnection.getresponse

    def _outer():
        return getattr(_tl, "inner", 0) > 0 or getattr(_tl, "u3", 0) > 0 or getattr(_tl, "ul", 0) > 0

    def request(self, method, url, body=None, headers=None, *a, **kw):
        if _outer():
            return orig_request(self, method, url, body, headers or {}, *a, **kw)
        path, _, qs = (url or "/").partition("?")
        query = dict(parse_qsl(qs)) if qs else None
        why_not = rec.gate(method.upper(), self.host, path, body if isinstance(body, (bytes, str)) else None)
        if why_not:
            raise rec.blocked("http", method.upper(), self.host, path, query, dict(headers or {}),
                              body if isinstance(body, (bytes, str)) else None, why_not)
        self._deedum_req = {"method": method.upper(), "host": self.host, "path": path or "/", "query": query,
                            "req_headers": dict(headers or {}),
                            "req_body": body if isinstance(body, (bytes, str)) else None, "t0": time.time()}
        try:
            return orig_request(self, method, url, body, headers or {}, *a, **kw)
        except Exception as e:   # refused connection, TLS failure: the attempt is still on the record
            pending, self._deedum_req = self._deedum_req, None
            t0 = pending.pop("t0")
            try:
                rec.record(dict(pending, transport="http", status=None, resp_body=None,
                            ms=int((time.time() - t0) * 1000), error=f"{type(e).__name__}: {e}"[:500]))
            except Exception as e2:   # a full disk must not replace the network error the caller handles
                sys.stderr.write(f"[deedum] record failed: {e2}\n")
            raise

    orig_putrequest, orig_putheader, orig_endheaders = hc.HTTPConnection.putrequest, hc.HTTPConnection.putheader, hc.HTTPConnection.endheaders

    def putrequest(self, method, url, *a, **kw):
        if not _outer() and getattr(self, "_deedum_req", None) is None:
            self._deedum_low = {"method": method, "url": url, "headers": {}}
        return orig_putrequest(self, method, url, *a, **kw)

    def putheader(self, header, *values):
        low = getattr(self, "_deedum_low", None)
        if low is not None:
            try:
                low["headers"][str(header)] = ", ".join(v.decode("latin-1") if isinstance(v, bytes) else str(v) for v in values)
            except Exception:
                pass
        return orig_putheader(self, header, *values)

    def endheaders(self, message_body=None, **kw):
        low = getattr(self, "_deedum_low", None)
        self._deedum_low = None
        if low is not None and not _outer() and getattr(self, "_deedum_req", None) is None:
            # built by hand (putrequest / putheader / endheaders): gate and record it like request()
            path, _, qs = (low["url"] or "/").partition("?")
            query = dict(parse_qsl(qs)) if qs else None
            body = message_body if isinstance(message_body, (bytes, str)) else None
            why_not = rec.gate(low["method"].upper(), self.host, path, body)
            if why_not:
                raise rec.blocked("http", low["method"].upper(), self.host, path, query, low["headers"], body, why_not)
            self._deedum_req = {"method": low["method"].upper(), "host": self.host, "path": path or "/", "query": query,
                                "req_headers": low["headers"], "req_body": body, "t0": time.time()}
        return orig_endheaders(self, message_body, **kw)

    def getresponse(self):
        pending = getattr(self, "_deedum_req", None)
        self._deedum_req = None
        if _outer() or pending is None:
            return orig_getresponse(self)
        t0 = pending.pop("t0")
        try:
            resp = orig_getresponse(self)
        except Exception as e:
            try:
                rec.record(dict(pending, transport="http", status=None, resp_body=None,
                            ms=int((time.time() - t0) * 1000), error=f"{type(e).__name__}: {e}"[:500]))
            except Exception as e2:   # a full disk must not replace the network error the caller handles
                sys.stderr.write(f"[deedum] record failed: {e2}\n")
            raise
        # The body is the caller's to read, possibly in chunks: copy what passes through read()
        # and write the record when it is finished or closed (once).
        chunks, done = [], [False]
        orig_read, orig_close = resp.read, resp.close
        limit = max(rec.cfg["write_response_max"], rec.cfg["read_response_max"])

        def flush():
            if done[0]:
                return
            done[0] = True
            _pending.discard(flush)
            try:
                rec.record(dict(pending, transport="http", status=resp.status, resp_body=b"".join(chunks),
                                ms=int((time.time() - t0) * 1000), error=None))
            except Exception as e:
                sys.stderr.write(f"[deedum] record failed: {e}\n")

        busy = [False]   # read(n) calls readinto() underneath: count the bytes once
        orig_readinto, orig_read1 = resp.readinto, getattr(resp, "read1", None)

        got = [0]

        def keep(data):
            if data and got[0] < limit:   # only up to the budget is kept, whatever size the read was
                part = bytes(data[: limit - got[0]])
                chunks.append(part); got[0] += len(part)

        def read(amt=None):
            busy[0] = True
            try:
                data = orig_read(amt)
            finally:
                busy[0] = False
            keep(data)
            # done when the whole body was asked for, nothing came back, or the stream hit EOF
            # (a read(n) of exactly Content-Length closes it without an empty read after)
            if amt is None or (not data and amt != 0) or resp.isclosed():
                flush()
            return data

        def readinto(b):
            n = orig_readinto(b)
            if not busy[0]:
                keep(memoryview(b)[:n])
                if (not n and len(b)) or resp.isclosed():
                    flush()
            return n

        def read1(n=-1):
            data = orig_read1(n)
            if not busy[0]:
                keep(data)
                if (not data and n != 0) or resp.isclosed():
                    flush()
            return data

        def close():
            flush()
            return orig_close()
        resp.read, resp.readinto, resp.close = read, readinto, close
        if orig_read1 is not None:
            resp.read1 = read1
        _pending.add(flush)   # a response never read nor closed is still recorded at exit
        return resp

    request._deedum = True
    hc.HTTPConnection.request, hc.HTTPConnection.getresponse = request, getresponse
    hc.HTTPConnection.putrequest, hc.HTTPConnection.putheader, hc.HTTPConnection.endheaders = putrequest, putheader, endheaders
    return True


def _hook_aiohttp(rec):
    try:
        import importlib.util
        if "aiohttp" not in sys.modules and importlib.util.find_spec("aiohttp") is None:
            return False
        import aiohttp
    except Exception:
        return False
    orig = aiohttp.ClientSession._request
    if getattr(orig, "_deedum", False):
        return True

    async def _request(self, method, str_or_url, **kw):
        url = str(str_or_url)
        base = getattr(self, "_base_url", None)
        if base is not None and "://" not in url:   # ClientSession(base_url=...) + session.post('/items')
            try:
                import yarl
                url = str(base.join(yarl.URL(url)))
            except Exception:
                pass
        host, path, q = _split(url)
        params = kw.get("params")   # session.delete(url, params={'id': 123}): part of the request, not of str_or_url
        if params:
            try:
                q = {**(q or {}), **{str(k): str(v) for k, v in (params.items() if hasattr(params, "items") else params)}}
            except Exception:
                pass
        body = kw.get("data")
        if kw.get("json") is not None:
            body = json.dumps(kw["json"], default=str)
        elif isinstance(body, dict):   # sent as application/x-www-form-urlencoded
            try:
                from urllib.parse import urlencode
                body = urlencode({k: v for k, v in body.items()}, doseq=True)
            except Exception:
                body = None
        body = body if isinstance(body, (bytes, str)) else None
        headers = dict(kw.get("headers") or {})
        why_not = rec.gate(method.upper(), host, path, body)
        if why_not:
            raise rec.blocked("http", method.upper(), host, path, q, headers, body, why_not)
        t0 = time.time()
        base_rec = {"transport": "http", "method": method.upper(), "host": host, "path": path, "query": q,
                    "req_headers": headers, "req_body": body}
        try:
            resp = await orig(self, method, str_or_url, **kw)
        except Exception as e:
            try:
                rec.record(dict(base_rec, status=None, resp_body=None, ms=int((time.time() - t0) * 1000), error=f"{type(e).__name__}: {e}"[:500]))
            except Exception as e2:   # a full disk must not replace the network error the caller handles
                sys.stderr.write(f"[deedum] record failed: {e2}\n")
            raise
        # The body is the caller's: a streaming consumer (iter_chunked, content.read) must see
        # every byte, so nothing is read here. The record is written when the caller's own
        # read() finishes (with the body), or when the response is released / closed (without).
        done = [False]

        def flush(data=None):
            if done[0]:
                return
            done[0] = True
            _pending.discard(flush)
            try:
                rec.record(dict(base_rec, status=resp.status, resp_body=data, ms=int((time.time() - t0) * 1000), error=None,
                                req_headers={**headers, **dict(resp.request_info.headers)}))
            except Exception as e:
                sys.stderr.write(f"[deedum] record failed: {e}\n")
        _pending.add(flush)
        orig_read, orig_release, orig_close = resp.read, resp.release, resp.close

        async def read():
            data = await orig_read()
            flush(data)
            return data

        def release():
            flush()
            return orig_release()

        def close():
            flush()
            return orig_close()
        resp.read, resp.release, resp.close = read, release, close
        return resp
    _request._deedum = True
    aiohttp.ClientSession._request = _request
    return True


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
    if getattr(orig, "_deedum", False):
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
                sys.stderr.write(f"[deedum] record failed: {e}\n")
    urlopen._deedum = True
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
    if getattr(grpc, "_deedum", False):
        return True

    class Trail(grpc.UnaryUnaryClientInterceptor, grpc.UnaryStreamClientInterceptor):
        def __init__(self, target):
            self.host = target.split(":")[0]
        def _meta(self, details):
            return {k: v for k, v in (details.metadata or [])}
        def _gate(self, details, request):
            why_not = rec.gate("RPC", self.host, details.method, None, transport="grpc")
            if why_not:
                raise rec.blocked("grpc", "RPC", self.host, details.method, None, self._meta(details),
                                  json.dumps(_proto_to_obj(request), default=str), why_not)

        def intercept_unary_unary(self, continuation, details, request):
            self._gate(details, request)
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
            self._gate(details, request)
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
                sys.stderr.write(f"[deedum] record failed: {e}\n")

    orig_secure, orig_insecure = grpc.secure_channel, grpc.insecure_channel
    def secure_channel(target, *a, **kw):
        return grpc.intercept_channel(orig_secure(target, *a, **kw), Trail(target))
    def insecure_channel(target, *a, **kw):
        return grpc.intercept_channel(orig_insecure(target, *a, **kw), Trail(target))
    grpc.secure_channel, grpc.insecure_channel = secure_channel, insecure_channel
    grpc._deedum = True
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
        raise RuntimeError("deedum: no trail directory. Run `deedum init` or set DEEDUM_DIR.")
    rec = Recorder(d, why=why)
    _hook_requests(rec); _hook_httpx(rec); _hook_urllib(rec); _hook_grpc(rec); _hook_urllib3(rec)
    _hook_http_client(rec); _hook_aiohttp(rec)
    # libraries imported later: hook them on import
    import builtins
    orig_import = builtins.__import__
    def imp(name, *a, **kw):
        m = orig_import(name, *a, **kw)
        try:
            top = name.split(".")[0]
            if top in ("requests", "httpx", "grpc", "urllib3", "botocore", "aiohttp"):
                {"requests": _hook_requests, "httpx": _hook_httpx, "grpc": _hook_grpc,
                 "urllib3": _hook_urllib3, "botocore": _hook_urllib3, "aiohttp": _hook_aiohttp}[top](rec)
        except Exception:
            pass
        return m
    builtins.__import__ = imp
    started = store.now_iso()
    import atexit
    atexit.register(lambda: rec.finish(started=started))
    _installed = rec
    return rec
