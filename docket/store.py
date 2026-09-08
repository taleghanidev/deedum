"""Where records live and how they are found. Stdlib only."""
import os, json, re, subprocess, datetime, pathlib

DIRNAME = ".docket"
DEFAULT_CONFIG = {
    "auto_commit": False,
    "read_request_max": 16384,
    "read_response_max": 2048,
    "write_request_max": 262144,
    "write_response_max": 65536,
    # A POST whose path matches this is a read (report, query, search, status poll), not a change.
    "read_path_regex": r"(?i)(query|search|report|get[A-Z_/]|/get$|list|status|download|lookup|stream|whoami|/me$|token|oauth|auth)",
    # Explicit overrides win over the regex: [{"host": "glob", "path_regex": "...", "kind": "read|write"}]
    "rules": [],
    "redact_headers": ["authorization", "developertoken", "developer-token", "x-api-key", "api-key",
                       "apikey", "cookie", "set-cookie", "x-auth-token", "authenticationtoken",
                       "proxy-authorization", "x-goog-api-key"],
    "redact_body_keys": ["password", "secret", "access_token", "refresh_token", "client_secret",
                         "id_token", "api_key", "apikey", "token", "authorization"],
    # Bodies of calls to these paths are never stored (credential exchanges).
    "auth_path_regex": r"(?i)(oauth|/token|/auth\b|login|signin|credential|/session)",
    # Any `name=value` where name looks secret is scrubbed in text bodies, query strings and URLs.
    "scrub_regex": r"(?i)\b(token|access_token|refresh_token|id_token|secret|client_secret|password|passwd|pwd|sig|signature|api_key|apikey|key|auth|authorization|session|sessionid|bearer)=([^&\s\"'<>]+)",
    "ignore_hosts": ["localhost", "127.0.0.1", "*.sentry.io", "*.posthog.com", "*.datadoghq.com"],
}

README = """# .docket — API change trail

Every API call made under `docket run` is recorded here. **If you are an AI agent asked what was
changed in an external system (ad platforms, CRMs, DNS, anything reached over an API), read this
directory before guessing.**

## Layout
- `CHANGES.md` — one line per **write** (POST/PUT/PATCH/DELETE that is not a read-style query),
  newest at the bottom: time, service, method, path, status, the `--why` reason, and the record id.
  Start here.
- `calls/YYYY-MM-DD.jsonl` — every call, reads included, one JSON object per line. Fields:
  `id, ts, run, why, kind (read|write), transport (http|grpc), method, host, path, query,
  request (headers redacted, body parsed when JSON), status, response (truncated for reads),
  ms, error, cwd, argv`.
- `runs/YYYY-MM-DD.jsonl` — one line per `docket run`: the command, cwd, why, start/end,
  read/write counts, exit code.
- `config.json` — truncation limits, read/write classification rules, redaction lists.

## Reading it
- `docket log --since 7d --writes` — timeline of changes.
- `docket show <id>` — the full request and response of one call.
- `docket report --since 30d` — markdown summary grouped by day and service.
- Or just `grep`/`jq` the jsonl files; they are plain text.

Secrets (Authorization headers, tokens, keys) are redacted before writing. Response bodies of
reads are truncated; writes keep the full request and up to 64 KB of the response.
"""

def find_dir(explicit=None):
    """Resolve the trail directory: --dir, $DOCKET_DIR, a `.docket` dir or pointer file walking
    up from cwd, else None."""
    cand = explicit or os.environ.get("DOCKET_DIR")
    if cand:
        return pathlib.Path(os.path.expanduser(cand)).resolve()
    p = pathlib.Path.cwd().resolve()
    for d in [p, *p.parents]:
        t = d / DIRNAME
        if t.is_dir():
            return t
        if t.is_file():  # pointer file, like a git worktree's .git file
            return pathlib.Path(os.path.expanduser(t.read_text().strip())).resolve()
    return None

def init_dir(path):
    path = pathlib.Path(path).resolve()
    (path / "calls").mkdir(parents=True, exist_ok=True)
    (path / "runs").mkdir(exist_ok=True)
    cfg = path / "config.json"
    if not cfg.exists():
        cfg.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
    (path / "README.md").write_text(README)
    ch = path / "CHANGES.md"
    if not ch.exists():
        ch.write_text("# API changes\n\nOne line per write. Full records in `calls/`. Newest at the bottom.\n\n")
    return path

def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads((pathlib.Path(path) / "config.json").read_text()))
    except Exception:
        pass
    return cfg

def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")

def git_root(path):
    try:
        out = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True).stdout.strip()
        return pathlib.Path(out)
    except Exception:
        return None

def git_commit(trail_dir, message):
    root = git_root(trail_dir)
    if not root:
        return "not in a git repository — nothing committed"
    subprocess.run(["git", "-C", str(root), "add", "-A", str(trail_dir)], check=False)
    r = subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", message, "--", str(trail_dir)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return "nothing to commit" if "nothing to commit" in (r.stdout + r.stderr) else (r.stderr.strip() or r.stdout.strip())
    return subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%h %s"],
                          capture_output=True, text=True).stdout.strip()

def iter_records(trail_dir, sub="calls", since=None, until=None):
    d = pathlib.Path(trail_dir) / sub
    if not d.is_dir():
        return
    for f in sorted(d.glob("*.jsonl")):
        day = f.stem
        if since and day < since[:10]:
            continue
        if until and day > until[:10]:
            continue
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

def parse_since(s):
    """'10m', '12h', '7d', '2w' or '2026-09-01' -> ISO local datetime string."""
    if s and not re.fullmatch(r"\d+[mdhw]|\d{4}-\d{2}-\d{2}.*", s):
        raise SystemExit(f"docket: --since wants 10m, 12h, 7d, 2w or a date, not {s!r}")
    if not s:
        return None
    m = re.fullmatch(r"(\d+)([mdhw])", s)
    if m:
        n, u = int(m.group(1)), m.group(2)
        delta = {"m": datetime.timedelta(minutes=n), "d": datetime.timedelta(days=n),
                 "h": datetime.timedelta(hours=n), "w": datetime.timedelta(weeks=n)}[u]
        return (datetime.datetime.now().astimezone() - delta).isoformat(timespec="seconds")
    return s
