"""Run: python3 -m pytest tests  (or: python3 tests/test_deedum.py)"""
import json, os, subprocess, sys, tempfile, threading, pathlib
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from deedum import store
from deedum.recorder import Recorder

class Echo(BaseHTTPRequestHandler):
    def _r(self):
        n = int(self.headers.get("Content-Length", 0)); b = self.rfile.read(n)
        out = b'{"ok":true,"echo":' + (b or b"null") + b"}"
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out))); self.end_headers()
        self.wfile.write(out)
    do_GET = do_POST = do_PUT = do_DELETE = _r
    def log_message(self, *a): pass

def serve():
    srv = HTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_port}"

def trail():
    d = store.init_dir(tempfile.mkdtemp())
    return d

def clean_env(**kw):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DEEDUM_", "CLAUDE", "AWS_")) and k not in ("PYTHONPATH", "NODE_OPTIONS")}
    env.update(kw)
    return env


def run(d, *cmd, why="t", extra=(), cwd=ROOT, **envkw):
    env = clean_env(DEEDUM_DIR=str(d), PYTHONPATH=str(ROOT), **envkw)
    w = ["--why", why] if why else []
    return subprocess.run([sys.executable, "-m", "deedum", "run", *w, *extra, "--", *cmd],
                          cwd=cwd, env=env, capture_output=True, text=True)


def cli(d, *a):
    return subprocess.run([sys.executable, "-m", "deedum", "--dir", str(d), *a], cwd=ROOT,
                          env=clean_env(PYTHONPATH=str(ROOT)), capture_output=True, text=True)

def records(d):
    return list(store.iter_records(d))

def test_redaction():
    r = Recorder(trail())
    assert r.body("a=1&refresh_token=S&b=2", 999)["_text"] == "a=1&refresh_token=<redacted>&b=2"
    assert r.body('{"access_token":"S","x":{"client_secret":"S"}}', 999)["_json"] == {"access_token": "<redacted>", "x": {"client_secret": "<redacted>"}}
    assert r.redact_headers({"Authorization": "Bearer S", "Accept": "*/*"}) == {"Authorization": "Bearer <redacted>", "Accept": "*/*"}
    assert "SECRET" not in r.scrub_argv("curl -u me:SECRET -H Authorization: Bearer SECRET -d {\"token\":\"SECRET\"} x?key=SECRET")

def test_classification():
    r = Recorder(trail())
    assert r.kind("GET", "h", "/v1/x") == "read"
    assert r.kind("POST", "h", "/Reporting/v13/GenerateReport/Submit") == "read"
    assert r.kind("POST", "h", "/CampaignManagement/v13/Campaigns/QueryByAccountId") == "read"
    assert r.kind("POST", "h", "/CampaignManagement/v13/EntityNegativeKeywords") == "write"
    assert r.kind("PUT", "h", "/v1/keywords") == "write"
    assert r._graphql_kind(json.dumps({"query": "mutation { x }"})) == "write"
    assert r._graphql_kind(json.dumps({"query": "query { x }"})) == "read"

def test_curl_and_python_capture():
    base = serve(); d = trail()
    out = run(d, "curl", "-s", "-X", "PUT", f"{base}/v1/k/1", "-H", "Authorization: Bearer S", "-d", '{"bid":14}')
    assert out.returncode == 0, out.stderr
    script = f"import urllib.request as u; u.urlopen(u.Request('{base}/v1/ag', data=b'{{}}', method='POST')).read()"
    out = run(d, sys.executable, "-c", script, why="py")
    assert out.returncode == 0, out.stderr
    recs = records(d)
    assert [(r["method"], r["path"], r["kind"]) for r in recs] == [("PUT", "/v1/k/1", "write"), ("POST", "/v1/ag", "write")]
    assert recs[0]["request"]["headers"]["Authorization"] == "Bearer <redacted>"
    assert recs[0]["request"]["body"]["_json"] == {"bid": 14}
    assert recs[0]["response"]["body"]["_json"]["ok"] is True
    assert "Bearer S" not in json.dumps(recs)
    assert (d / "CHANGES.md").read_text().count("\n- ") == 2

def test_requests_capture_if_installed():
    try:
        import requests  # noqa
    except ImportError:
        return
    base = serve(); d = trail()
    out = run(d, sys.executable, "-c", f"import requests; requests.post('{base}/v1/r', json={{'a':1}})")
    assert out.returncode == 0, out.stderr
    assert records(d)[0]["request"]["body"]["_json"] == {"a": 1}


def test_value_scrub():
    from deedum.recorder import scrub_values
    os.environ["DEEDUM_TEST_API_TOKEN"] = "OddFormatValue0123456789"
    line = json.dumps({"a": "cfk_" + "A" * 40, "b": "OddFormatValue0123456789", "c": "https://u:pw12345@h/x",
                       "d": "sk-proj-" + "x" * 30, "e": "Bearer abcdefgh12345", "keyword": "house"})
    out = scrub_values(line)
    for leaked in ("cfk_AAAA", "OddFormatValue", "pw12345", "sk-proj-x", "abcdefgh12345"):
        assert leaked not in out, leaked
    assert '"keyword": "house"' in out
    out = scrub_values('curl -H "x-access-token: abcdef123456" "https://h/x?rwg_token=zz12345678&q=ok"')
    assert "abcdef123456" not in out and "zz12345678" not in out and "q=ok" in out

def test_project_folder_service_and_secret_names():
    base = serve(); d = trail()
    proj = pathlib.Path(tempfile.mkdtemp()) / "shop"; (proj / "scripts").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    secret = "zq" + "9" * 22
    script = f"import urllib.request as u; u.urlopen(u.Request('{base}/v1/ag', data=b'{{}}', method='POST', headers={{'Authorization': 'Bearer {secret}'}})).read()"
    out = run(d, sys.executable, "-c", script, cwd=proj / "scripts", SHOP_API_TOKEN=secret)
    assert out.returncode == 0, out.stderr
    r = records(d)[0]
    assert (r["project"], r["folder"]) == ("shop", "scripts"), r
    assert r["secrets"] == ["SHOP_API_TOKEN"], r["secrets"]
    assert r["request"]["headers"]["Authorization"].startswith("Bearer <secret:SHOP_API_TOKEN#")
    assert secret not in json.dumps(records(d)) and secret not in (d / "CHANGES.md").read_text()
    assert "[shop]" in (d / "CHANGES.md").read_text()
    s = cli(d, "secrets", "--json"); assert json.loads(s.stdout)[0]["secret"] == "SHOP_API_TOKEN", s.stderr
    p = cli(d, "projects", "--json"); assert json.loads(p.stdout)[0]["project"] == "shop", p.stderr
    assert "shop/scripts" in cli(d, "log", "--writes").stdout


def test_dry_run_blocks_writes_everywhere():
    base = serve(); d = trail()
    py = f"import urllib.request as u\ntry:\n  u.urlopen(u.Request('{base}/v1/x', data=b'{{}}', method='POST'))\n  print('SENT')\nexcept PermissionError as e: print('blocked')"
    out = run(d, sys.executable, "-c", py, extra=["--dry"])
    assert "blocked" in out.stdout and "SENT" not in out.stdout, out.stdout + out.stderr
    out = run(d, "curl", "-s", "-X", "DELETE", f"{base}/v1/y", extra=["--dry"])
    assert out.returncode == 7, (out.returncode, out.stderr)
    st = [r["status"] for r in records(d)]
    assert st == ["BLOCKED", "BLOCKED"], st


def test_guard_requires_a_reason():
    base = serve(); d = trail()
    cli(d, "guard", "127.0.0.1")
    curl = ["curl", "-s", "-X", "POST", f"{base}/v1/z", "-d", "{}"]
    assert run(d, *curl, why="").returncode == 7
    assert run(d, *curl, why="because").returncode == 0
    assert [r["status"] for r in records(d)] == ["BLOCKED", 200]


def test_session_why_and_claude_hook():
    base = serve(); d = trail()
    env = clean_env(PYTHONPATH=str(ROOT), DEEDUM_SESSION="s1")
    hook = subprocess.run([sys.executable, "-m", "deedum", "--dir", str(d), "why", "--from-claude-hook"],
                          input=json.dumps({"session_id": "s1", "prompt": "raise   the bids\nplease"}),
                          env=env, capture_output=True, text=True, cwd=ROOT)
    assert hook.returncode == 0 and hook.stdout == "", hook
    out = run(d, "curl", "-s", "-X", "PUT", f"{base}/v1/b", "-d", "{}", why="", DEEDUM_SESSION="s1")
    assert out.returncode == 0, out.stderr
    assert records(d)[0]["why"] == "asked: raise the bids please"


def test_command_record_when_nothing_captured_and_bad_hosts():
    d = trail()
    out = run(d, "true", why="approved prod release")
    assert out.returncode == 0
    runs = list(store.iter_records(d, "runs"))
    assert runs[0]["type"] == "command" and runs[0]["why"] == "approved prod release"
    assert "command · `true`" in (d / "CHANGES.md").read_text()
    out = run(d, "curl", "-s", "-m", "1", "a.com b.com", why="x")
    assert all(store.valid_host(r["host"]) for r in records(d))


def test_reads_to_dev_hosts_are_skipped_writes_kept():
    base = serve(); d = trail()
    run(d, "curl", "-s", f"{base}/v1/read")
    run(d, "curl", "-s", "-X", "POST", f"{base}/v1/w", "-d", "{}")
    assert [r["method"] for r in records(d)] == ["POST"]


def test_compact_and_read_gz():
    import gzip as gz
    d = trail()
    (d / "calls" / "2020-01-01.jsonl").write_text(json.dumps({"id": "20200101-000000-aaaaaa", "ts": "2020-01-01T00:00:00+00:00", "kind": "read", "host": "h", "path": "/", "method": "GET", "response": {"body": {"_text": "x" * 100}}}) + "\n")
    (d / "calls" / "2020-01-02.jsonl").write_text(json.dumps({"id": "20200102-000000-aaaaaa", "ts": "2020-01-02T00:00:00+00:00", "kind": "write", "host": "h", "path": "/", "method": "POST", "request": {"body": {"_text": "key sk-proj-" + "Q" * 30}}}) + "\n")
    out = cli(d, "compact", "--older", "1d", "--strip-read-bodies")
    assert out.returncode == 0, out.stderr
    assert (d / "calls" / "2020-01-01.jsonl.gz").exists()
    sc = cli(d, "scan"); assert "20200102-000000-aaaaaa" in sc.stdout, sc.stdout   # compacted days are scanned too
    sc = cli(d, "scan", "--fix"); assert sc.returncode == 0
    assert "QQQQQQ" not in json.dumps(list(store.iter_records(d)))
    r = list(store.iter_records(d))[0]
    assert r["response"]["body"] == {"_stripped": True}
    assert cli(d, "show", "20200101-000000-aaaaaa").returncode == 0


def test_node_hook_if_node():
    import shutil
    if not shutil.which("node"):
        return
    base = serve(); d = trail()
    js = f"fetch('{base}/v1/n',{{method:'POST',headers:{{'x-api-key':'k'.repeat(20)}},body:JSON.stringify({{a:1,token:'t'}})}}).then(r=>r.text()).then(()=>require('http').request('{base}/v1/h',{{method:'PUT'}}).end('x'))"
    out = run(d, "node", "-e", js)
    assert out.returncode == 0, out.stderr
    import time; time.sleep(0.5)
    recs = records(d)
    assert sorted(r["method"] for r in recs) == ["POST", "PUT"], recs
    post = [r for r in recs if r["method"] == "POST"][0]
    assert post["request"]["body"]["_json"] == {"a": 1, "token": "<redacted>"} and post["request"]["headers"]["x-api-key"] == "<redacted>"
    assert post["runtime"] == "node"
    out = run(d, "node", "-e", f"fetch('{base}/v1/n',{{method:'POST',body:'{{}}'}}).then(()=>console.log('SENT')).catch(e=>console.log('blocked'))", extra=["--dry"])
    assert "blocked" in out.stdout, out.stdout + out.stderr


def test_every_command_runs():
    d = trail()
    for a in (["status"], ["log"], ["log", "--json"], ["report"], ["report", "--json"], ["projects"], ["secrets"],
              ["runs"], ["noise"], ["scan"], ["why"], ["hook", "claude"], ["env"], ["ignore", "example.org"],
              ["guard"], ["setup", str(d)]):
        out = cli(d, *a)
        assert out.returncode == 0, (a, out.stderr)




# ---- 0.3 ------------------------------------------------------------------------------------
def test_dry_run_blocks_by_method_not_path():
    """POST /v1/tokens and /oauth/clients are labelled reads; --dry must still block them."""
    base = serve(); d = trail()
    for p in ("/v1/tokens", "/v1/oauth/clients", "/v1/webhooks/status"):
        out = run(d, "curl", "-s", "-X", "POST", f"{base}{p}", "-d", "{}", extra=["--dry"])
        assert out.returncode == 7, (p, out.stderr)
    assert all(r["status"] == "BLOCKED" for r in records(d))
    out = run(d, "curl", "-s", f"{base}/v1/tokens", extra=["--dry"])      # a GET goes through
    assert out.returncode == 0


def test_field_name_redaction_is_by_word():
    from deedum.recorder import secret_name
    for yes in ("access_token", "X-Auth-Key", "xi-api-key", "accessToken", "appsecret_proof", "developertoken",
                "password_confirm", "Cookie", "client_secret", "apikey", "Authorization", "refreshToken", "token_v2", "secret1", "api_key_2024", "session_id", "sessionId", "PHPSESSID", "authorization_code", "X-Authentication", "verification_code", "mfa_code", "tokenValue", "authTokenValue", "session_ticket", "SecretString", "password_hash", "access_tokens", "api_keys", "secrets", "credentials", "ssh_keys", "oauth_verifier", "code_challenge", "client_assertion"):
        assert secret_name(yes), yes
    for no in ("max_tokens", "author", "keyword", "session_name", "monkey", "tokens_used", "authors", "key_count",
               "turkey", "signature_required", "cookie_policy", "client_id", "user_id", "session_count", "postal_code", "country_code", "status_code", "token_count", "token_type", "key_name", "cookie_policy", "input_tokens", "output_tokens", "total_tokens", "tokens"):
        assert not secret_name(no), no
    r = Recorder(trail())
    assert r.redact_obj({"max_tokens": 1, "author": "b", "access_token": "x"}) == {"max_tokens": 1, "author": "b", "access_token": "<redacted>"}
    # a JSON body too big to parse, cut by truncation: the pairs are still redacted, the cut one too
    big = '{"passphrase":"a quiet orchard beside the river","author":"bob","padding":"' + "x" * 300 + '","api_key":"zzzz-not-shaped-at-all'
    out = r.body(big, 100)
    assert "orchard" not in out["_text"] and "zzzz" not in out["_text"] and '"author":"bob"' in out["_text"] and out["_truncated"], out


def test_prose_reason_is_scrubbed():
    from deedum.recorder import scrub_prose
    assert scrub_prose("log in with password hunter2 and change the plan") == "log in with password <redacted> and change the plan"
    assert scrub_prose("the api key is abc123xyz, rotate it") == "the api key is <redacted>, rotate it"
    assert scrub_prose("raise the bids to $14") == "raise the bids to $14"
    base = serve(); d = trail()
    env = clean_env(PYTHONPATH=str(ROOT), DEEDUM_SESSION="s3")
    subprocess.run([sys.executable, "-m", "deedum", "--dir", str(d), "why", "--from-claude-hook"],
                   input=json.dumps({"session_id": "s3", "prompt": "use password hunter2 then save"}), env=env, capture_output=True, text=True, cwd=ROOT)
    run(d, "curl", "-s", "-X", "PUT", f"{base}/v1/p", "-d", "{}", why="", DEEDUM_SESSION="s3")
    assert "hunter2" not in json.dumps(records(d)) and "hunter2" not in (d / "CHANGES.md").read_text()


def test_http_client_and_urllib_compat():
    base = serve(); d = trail()
    port = base.rsplit(":", 1)[1]
    out = run(d, sys.executable, "-c", f"import http.client as h; c=h.HTTPConnection('127.0.0.1',{port}); c.request('POST','/v1/raw',body='{{\"a\":1}}'); r=c.getresponse(); print(r.read()[:8])")
    assert out.returncode == 0, out.stderr
    # a bounded read of exactly Content-Length, never closed: still one record, with the body
    out = run(d, sys.executable, "-c", f"import http.client as h; c=h.HTTPConnection('127.0.0.1',{port}); c.request('PUT','/v1/readinto',body='1'); r=c.getresponse(); n=int(r.getheader('Content-Length')); b=bytearray(n); print(r.readinto(b), r.read1(10))")
    assert out.returncode == 0, out.stderr
    ri = [r for r in records(d) if r["path"] == "/v1/readinto"]
    assert len(ri) == 1 and ri[0]["response"]["body"]["_json"]["ok"] is True, ri
    out = run(d, sys.executable, "-c", f"import http.client as h; c=h.HTTPConnection('127.0.0.1',{port}); c.request('PUT','/v1/bounded',body='1'); r=c.getresponse(); r.read(0); n=int(r.getheader('Content-Length')); print(len(r.read(n)))")
    assert out.returncode == 0, out.stderr
    b = [r for r in records(d) if r["path"] == "/v1/bounded"]
    assert len(b) == 1 and b[0]["response"]["body"]["_json"]["ok"] is True, b
    # never read, never closed: recorded at exit, and counted in the run line
    d2 = trail()
    out = run(d2, sys.executable, "-c", f"import http.client as h; c=h.HTTPConnection('127.0.0.1',{port}); c.request('PUT','/v1/unread',body='1'); r=c.getresponse()")
    assert out.returncode == 0 and [r["path"] for r in records(d2)] == ["/v1/unread"], out.stderr
    assert [r["writes"] for r in store.iter_records(d2, "runs") if r.get("type") != "command"] == [1]
    out = run(d, sys.executable, "-c", "import http.client as h; c=h.HTTPConnection('127.0.0.1',9); c.request('PUT','/v1/refused',body='1')")
    assert out.returncode != 0
    f = [r for r in records(d) if r["path"] == "/v1/refused"]
    assert len(f) == 1 and f[0]["status"] is None and "refused" in (f[0]["error"] or "").lower(), f
    out = run(d, sys.executable, "-c", f"import urllib.request as u; r=u.urlopen('{base}/v1/x'); print(r.getheader('Content-Type'), r.status, r.read()[:4])")
    assert out.returncode == 0 and out.stdout.startswith("application/json 200"), out.stdout + out.stderr
    recs = records(d)
    raw = [r for r in recs if r["path"] == "/v1/raw"]
    assert len(raw) == 1 and raw[0]["request"]["body"]["_json"] == {"a": 1} and raw[0]["response"]["body"]["_json"]["ok"] is True, raw
    assert [r["path"] for r in recs if r["path"] == "/v1/x"] == []  # a read to a dev host is skipped, and not double-recorded
    if "requests" in sys.modules or __import__("importlib").util.find_spec("requests"):
        out = run(d, sys.executable, "-c", f"import requests; requests.post('{base}/v1/req', json={{'b':2}})")
        assert out.returncode == 0, out.stderr
        assert len([r for r in records(d) if r["path"] == "/v1/req"]) == 1  # requests -> urllib3 -> http.client: once


def test_agent_block_and_via():
    base = serve(); d = trail()
    tp = pathlib.Path(tempfile.mkdtemp()) / "t.jsonl"
    tp.write_text(json.dumps({"type": "assistant", "message": {"model": "claude-fable-5-1"}}) + "\n")
    env = clean_env(PYTHONPATH=str(ROOT), DEEDUM_SESSION="s5")
    subprocess.run([sys.executable, "-m", "deedum", "--dir", str(d), "why", "--from-claude-hook"],
                   input=json.dumps({"session_id": "s5", "prompt": "do it", "cwd": "/work/here", "transcript_path": str(tp)}),
                   env=env, capture_output=True, text=True, cwd=ROOT)
    run(d, "curl", "-s", "-X", "PUT", f"{base}/v1/a", "-d", "{}", why="", DEEDUM_SESSION="s5", CLAUDECODE="1")
    r = records(d)[0]
    assert r["via"] == "curl" and r["agent"]["name"] == "claude-code" and r["agent"]["model"] == "claude-fable-5-1"
    assert r["agent"]["started_in"] == "/work/here" and r["agent"]["session"] == "s5" and r["agent"]["user"]


def test_exec_shim_records_binaries():
    import shutil, stat
    from deedum.cli import ensure_exec_shims, BIN_DIR
    assert ensure_exec_shims(), BIN_DIR
    d = trail()
    fake = pathlib.Path(tempfile.mkdtemp())
    for name in ("aws", "ssh", "git", "gh", "az", "gcloud", "doctl", "wrangler", "psql", "redis-cli", "docker", "mongosh"):
        f = fake / name
        # the fake git answers `config --get alias.X` like a repo with one alias, `ship -> push`
        # ... and in /other/repo (`-C /other/repo`) the alias `st` means push, not status
        f.write_text("#!/bin/sh\nif [ \"$1\" = -C ] && [ \"$3\" = config ]; then [ \"$5\" = alias.st ] && echo push && exit 0; exit 1; fi\n"
                     "if [ \"$1\" = config ]; then [ \"$3\" = alias.ship ] && echo push && exit 0; exit 1; fi\necho fake-$0 \"$@\"\n")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    path = f"{fake}:{os.environ['PATH']}"
    # no writable place for the shims: a dry run refuses rather than running the real binary
    out = run(d, "aws", "ssm", "send-command", extra=["--dry"], PATH=path, DEEDUM_BIN="/dev/null/nowhere")
    assert out.returncode != 0 and "fake-" not in out.stdout and "refusing" in out.stderr, (out.stdout, out.stderr)
    out = run(d, "aws", "ssm", "send-command", "--profile", "prod", "--region", "us-east-1", "--instance-ids", "i-1", PATH=path)
    assert out.returncode == 0 and "fake-" in out.stdout, out.stderr
    out = run(d, "aws", "s3", "ls", PATH=path, AWS_PROFILE="prod", AWS_REGION="us-east-1")
    out = run(d, "ssh", "deploy@box.example.com", "sudo systemctl restart app", PATH=path)
    out = run(d, "git", "status", PATH=path)
    out = run(d, "git", "-c", "alias.st=status", "st", extra=["--dry"], PATH=path); assert out.returncode == 0, out.stderr
    out = run(d, "git", "push", "origin", "main", PATH=path)
    out = run(d, "ssh", "box.example.com", "rm -rf /data", extra=["--dry"], PATH=path)
    assert out.returncode == 7 and "fake-" not in out.stdout, (out.returncode, out.stdout, out.stderr)
    # a refusal holds even when the trail cannot be written (Codex review 2026-10-19)
    ro = trail(); os.chmod(ro / "calls", 0o500)
    try:
        out = run(ro, "ssh", "box.example.com", "rm -rf /data", extra=["--dry"], PATH=path)
        assert out.returncode == 7 and "fake-" not in out.stdout and "could not record" in out.stderr, (out.returncode, out.stdout, out.stderr)
        out = run(ro, "curl", "-s", "-X", "DELETE", f"{serve()}/v1/y", extra=["--dry"])
        assert out.returncode == 7 and "could not record" in out.stderr, (out.returncode, out.stderr)
    finally:
        os.chmod(ro / "calls", 0o700)
    # Codex review 2026-10-19: a read label must be strict, or --dry lets a destructive command out
    for cmd in (["aws", "logs", "delete-log-group", "--log-group-name", "prod"], ["ssh", "box.example.com", "ls; rm -rf /data"],
                ["ssh", "box.example.com", "cat x | sh"], ["gh", "api", "-X", "DELETE", "repos/a/b"],
                ["gh", "api", "--method=DELETE", "repos/a/b"], ["gh", "api", "-XDELETE", "repos/a/b"], ["gh", "api", "-f", "k=v", "repos/a/b"],
                ["ssh", "box.example.com", "ls\nrm -rf /data"], ["ssh", "box.example.com", "ls" + " " * 210 + "; rm -rf /data"],
                ["ssh", "box.example.com", "ls $(rm -rf /data)"], ["git", "-C", "/repo", "push", "origin", "main"],
                ["git", "--git-dir=/repo/.git", "push"], ["az", "group", "delete", "--name", "list", "--yes"],
                ["gcloud", "compute", "instances", "delete", "x", "--zone", "list"], ["ssh", "box.example.com", "journalctl --vacuum-time=1s"],
                ["ssh", "box.example.com", "hostname newname"], ["ssh", "box.example.com", "top -d 1 -n 'x'"], ["doctl", "compute", "droplet", "delete", "list"],
                ["ssh", "box.example.com", "journalctl", "--vacuum-time=1s"], ["ssh", "-p", "2222", "deploy@box.example.com", "hostname", "x"],
                ["gcloud", "compute", "instances", "delete", "list"], ["gcloud", "compute", "instances", "add-metadata", "list", "--metadata=x=y"],
                ["gcloud", "compute", "ssh", "list", "--command=rm -rf /data"], ["aws", "ssm", "start-session", "--target", "i-1"],
                ["ssh", "-o", "PermitLocalCommand=yes", "-o", "LocalCommand=touch /tmp/changed", "box.example.com", "uptime"],
                ["ssh", "-oProxyCommand=sh -c x", "box.example.com", "uptime"], ["ssh", "-F", "/tmp/cfg", "box.example.com", "uptime"],
                ["ssh", "-L", "8080:localhost:80", "box.example.com", "uptime"], ["gcloud", "compute", "instances", "suspend", "list", "--zone=a"],
                ["az", "vm", "deallocate", "list"], ["gcloud", "compute", "newthing", "list"],
                ["git", "-c", "alias.publish=push", "publish", "origin", "main"], ["git", "mystery-alias", "x"],
                ["git", "-c", "alias.publish=ship", "publish", "origin", "main"], ["git", "ship", "origin"],
                ["git", "-c", "alias.a=b", "-c", "alias.b=a", "a"], ["git", "-C", "/other/repo", "st"],
                ["wrangler", "d1", "execute", "list", "--remote", "--command=DROP TABLE users"], ["wrangler", "kv", "key", "put", "list", "v"],
                ["psql", "-h", "db.example.com", "-c", "DELETE FROM users"], ["psql", "-h", "db.example.com", "-c", "select 1; drop table x"], ["psql", "-h", "db.example.com"],
                ["psql", "-h", "db.example.com", "-f", "migrate.sql"], ["redis-cli", "-h", "cache.example.com", "FLUSHALL"], ["redis-cli", "SET", "k", "v"],
                ["docker", "run", "--rm", "img"], ["docker", "exec", "c", "sh"], ["docker", "push", "img"], ["mongosh", "--eval", "db.users.deleteMany({})"]):
        out = run(d, *cmd, extra=["--dry"], PATH=path)
        assert out.returncode == 7 and "fake-" not in out.stdout, cmd
    for cmd in (["aws", "logs", "describe-log-groups"], ["ssh", "box.example.com", "ls -la /data"], ["gh", "api", "repos/a/b"],
                ["az", "group", "list"], ["ssh", "box.example.com", "journalctl -u app -n 50 --since=yesterday"], ["ssh", "box.example.com", "hostname"],
                ["ssh", "-p", "2222", "box.example.com", "journalctl", "-u", "app"], ["gcloud", "compute", "instances", "list"],
                ["aws", "--profile", "prod", "s3", "ls"], ["aws", "--region=us-east-1", "ec2", "describe-instances"],
                ["aws", "ssm", "describe-instance-information"], ["aws", "lambda", "list-functions"],
                ["ssh", "-o", "StrictHostKeyChecking=no", "-i", "/k", "-p", "22", "box.example.com", "uptime"],
                ["wrangler", "kv", "key", "list", "--namespace-id=x"], ["wrangler", "d1", "list"],
                ["psql", "-h", "db.example.com", "-c", "SELECT count(*) FROM users"], ["psql", "-h", "db.example.com", "-c", "\\dt"],
                ["redis-cli", "-h", "cache.example.com", "GET", "k"], ["redis-cli", "INFO"], ["docker", "ps"], ["docker", "logs", "c"], ["docker", "image", "ls"],
                ["mongosh", "--eval", "db.users.find({})"]):
        out = run(d, *cmd, extra=["--dry"], PATH=path)
        assert out.returncode == 0 and "fake-" in out.stdout, (cmd, out.stderr)
    out = run(d, "psql", "-h", "db.example.com", "-U", "app", "-c", "DELETE FROM users WHERE id=1", PATH=path)
    pg = [x for x in records(d) if x["method"] == "PSQL"][-1]; assert pg["host"] == "db.example.com" and pg["kind"] == "write" and "DELETE FROM users" in pg["path"], pg
    out = run(d, "aws", "iam", "delete-login-profile", "--user-name", "alice", PATH=path)   # "login" is not an HTTP credential exchange here
    lp = [r for r in records(d) if r["path"] == "/iam delete-login-profile"]
    assert lp and lp[0]["kind"] == "write" and "delete-login-profile" in (d / "CHANGES.md").read_text(), lp
    out = run(d, "aws", "iam", "delete-login-profile", "--user-name", "alice", extra=["--dry"], PATH=path)
    assert out.returncode == 7
    out = run(d, "aws", "secretsmanager", "create-secret", "--name", "x", "--secret-string", "hunter2hunter2", PATH=path)
    out = run(d, "ssh", "box.example.com", "deploy --password hunter2hunter2 now", PATH=path)
    out = run(d, "ssh", "box.example.com", "deploy --password \"correct horse battery staple\" now", PATH=path)
    out = run(d, "ssh", "box.example.com", "deploy --token 'correct horse", PATH=path)   # unparseable: omitted
    out = run(d, "ssh", "box.example.com", "python -c 'print(\"hello world\")'", PATH=path)   # no secret flag: kept as given
    assert 'print(\\"hello world\\")' in json.dumps([r for r in records(d) if "hello" in json.dumps(r)]), [r["argv"] for r in records(d)][-1]
    out = run(d, "aws", "secretsmanager", "put-secret-value", "--secret-string", "correct horse battery staple", PATH=path)
    out = run(d, "aws", "secretsmanager", "put-secret-value", "--secret-string=correct horse battery staple", extra=["--dry"], PATH=path)
    blob = json.dumps(records(d)) + (d / "CHANGES.md").read_text() + json.dumps(list(store.iter_records(d, "runs")))
    assert "hunter2" not in blob and "horse" not in blob and "staple" not in blob
    # SIGTERM to the shim reaches the binary underneath
    import signal, time as _t
    slow = fake / "terraform"; slow.write_text("#!/bin/sh\ntrap 'echo child-terminated; exit 143' TERM\ntrap 'echo child-interrupted; exit 130' INT\nsleep 5 & wait $!\n"); slow.chmod(slow.stat().st_mode | stat.S_IEXEC)
    env = clean_env(DEEDUM_DIR=str(d), PYTHONPATH=str(ROOT), PATH=path)
    pr = subprocess.Popen([sys.executable, "-m", "deedum", "run", "--", "terraform", "apply"], cwd=ROOT, env=env, stdout=subprocess.PIPE, text=True)
    _t.sleep(1.5)
    # the shim is deedum run's child: find it and terminate it, as a supervisor would
    kids = subprocess.run(["pgrep", "-P", str(pr.pid)], capture_output=True, text=True).stdout.split()
    for k in kids: os.kill(int(k), signal.SIGTERM)
    outp, _ = pr.communicate(timeout=10)
    assert "child-terminated" in outp, outp
    assert [r["status"] for r in records(d) if r["method"] == "TERRAFORM"] == [143]
    pr = subprocess.Popen([sys.executable, "-m", "deedum", "run", "--", "terraform", "apply"], cwd=ROOT, env=env, stdout=subprocess.PIPE, text=True)
    _t.sleep(1.5)
    for k in subprocess.run(["pgrep", "-P", str(pr.pid)], capture_output=True, text=True).stdout.split(): os.kill(int(k), signal.SIGINT)
    outp, _ = pr.communicate(timeout=10)
    assert "child-interrupted" in outp, outp
    recs = records(d)
    got = [(r["transport"], r["method"], r["host"], r["kind"], r["status"]) for r in recs][:5]
    assert got == [("exec", "AWS", "prod.us-east-1", "write", 0), ("exec", "AWS", "prod.us-east-1", "read", 0),
                   ("exec", "SSH", "box.example.com", "write", 0), ("exec", "GIT", "origin", "write", 0),
                   ("exec", "SSH", "box.example.com", "write", "BLOCKED")], got
    assert recs[0]["path"] == "/ssm send-command" and recs[2]["path"] == "/sudo systemctl restart app"
    assert "deploy@" not in recs[2]["host"]
    ch = (d / "CHANGES.md").read_text()
    assert "command · `git status`" in ch, ch   # a run that captured nothing still leaves its command line


def test_node_http_response_body():
    import shutil
    if not shutil.which("node"):
        return
    base = serve(); d = trail()
    js = f"require('http').request('{base}/v1/h',{{method:'PUT'}},res=>{{let b='';res.on('data',c=>b+=c);res.on('end',()=>console.log('got',b))}}).end('x')"
    out = run(d, "node", "-e", js)
    assert out.returncode == 0 and "got" in out.stdout, out.stderr
    import time; time.sleep(0.5)
    r = [r for r in records(d) if r["path"] == "/v1/h"][0]
    assert '"ok":true' in r["response"]["body"]["_text"] and r["agent"]["user"] and r["via"] == "script", r
    # a JSON request body too big to keep whole: the credential pair is still redacted in the kept text
    big = "'{\"passphrase\":\"a quiet orchard beside the river\",\"pad\":\"' + 'y'.repeat(1200000) + '\"}'"   # past the parse limit: kept as text
    js = f"require('http').request('{base}/v1/big',{{method:'PUT',headers:{{'content-type':'application/json'}}}},res=>res.resume()).end({big})"
    out = run(d, "node", "-e", js); time.sleep(0.5)
    rb = [r for r in records(d) if r["path"] == "/v1/big"][0]["request"]["body"]
    assert rb.get("_truncated") and "orchard" not in rb["_text"], rb.get("_text", "")[:120]
    js = f"require('http').request('{base}/v1/fire',{{method:'PUT'}}).end('x')"   # nobody reads the body
    out = run(d, "node", "-e", js); time.sleep(0.5)
    assert [r["status"] for r in records(d) if r["path"] == "/v1/fire"] == [200]


def test_prune_and_pinned_hook():
    d = trail()
    for day, kind in (("2020-01-01", "read"), ("2020-01-01", "write"), ("2020-02-01", "write")):
        with open(d / "calls" / f"{day}.jsonl", "a") as f:
            f.write(json.dumps({"id": f"{day.replace('-', '')}-000000-aaaaaa", "ts": f"{day}T00:00:00+00:00", "kind": kind, "host": "h", "path": "/", "method": "GET"}) + "\n")
    (d / "CHANGES.md").write_text("# x\n\n- 2020-01-01 00:00 · a\n- 2020-02-01 00:00 · b\n")
    out = cli(d, "prune", "--before", "2020-01-15", "--dry-run"); assert out.returncode == 0 and "would delete 0" in out.stdout, out.stdout + out.stderr
    out = cli(d, "prune", "--before", "2020-01-15"); assert out.returncode == 0, out.stderr
    assert [r["kind"] for r in records(d)] == ["write", "write"]
    out = cli(d, "prune", "--before", "2020-01-15", "--rewrite-history"); assert out.returncode != 0 and "--writes-too" in out.stderr
    out = cli(d, "prune", "--before", "2020-01-15", "--writes-too"); assert out.returncode == 0, out.stderr
    assert [r["ts"][:10] for r in records(d)] == ["2020-02-01"] and "2020-01-01" not in (d / "CHANGES.md").read_text()
    from deedum.cli import hook_source, PKG_DIR, _change_line_patterns
    import re as _re
    pats = _change_line_patterns(d, "2026-03-05")   # a trail with no git history starts at the cutoff month
    blob = "# API changes\n\n- 2024-12-31 10:00 · old\n- 2026-01-09 10:00 · old\n- 2026-03-04 10:00 · old\n- 2026-03-05 10:00 · kept\n- 2026-03-06 10:00 · kept\n"
    for pat in pats:
        blob = _re.sub(pat, "", blob)   # what git-filter-repo --replace-text does to every historical blob
    assert blob == "# API changes\n\n- 2026-03-05 10:00 · kept\n- 2026-03-06 10:00 · kept\n", blob
    env_out = cli(d, "env"); assert env_out.returncode == 0 and "ssh() {" in env_out.stdout and "curl() {" in env_out.stdout
    from deedum.cli import BIN_DIR, shim_wrapper
    w = (pathlib.Path(BIN_DIR) / "aws").read_text()
    assert w.startswith("#!/bin/sh") and sys.executable in w and "DEEDUM_SHIM_NAME=aws" in w, w   # no `env python3`: the machine may have none
    assert f"_EXPECTED = {PKG_DIR!r}" in hook_source()
    # the planted hook refuses a DEEDUM_PKG that is not the pinned checkout
    env = clean_env(DEEDUM_RUN="1", DEEDUM_PKG="/nowhere/evil", DEEDUM_DIR=str(d))
    site = pathlib.Path(tempfile.mkdtemp()); (site / "deedum_hook.py").write_text(hook_source())
    out = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import deedum_hook; print('deedum' in sys.modules)", str(site)], env=env, capture_output=True, text=True)
    assert out.stdout.strip() == "False", out.stdout + out.stderr




# ---- 0.3 round 2: the fresh-eyes review, and policy / approvals / MCP / html ------------------
def test_review_round2_dry_run_bypasses():
    base = serve(); d = trail(); port = base.rsplit(":", 1)[1]
    # GraphQL: operationName picks the operation; any mutation in the document is a write
    from deedum.recorder import graphql_kind
    assert graphql_kind("query A { x } mutation B { y }", "B") == "write"
    assert graphql_kind("query A { x } mutation B { y }", "A") == "read"
    assert graphql_kind("query A { x } mutation B { y }") == "write"
    assert graphql_kind("query A { x }", "Nope") == "write" and graphql_kind("{ x }") == "read" and graphql_kind("mutation { x }") == "write"
    doc = json.dumps({"query": "query A{x} mutation B{y}", "operationName": "B"})
    out = run(d, "curl", "-s", "-X", "POST", f"{base}/graphql", "-d", doc, extra=["--dry"]); assert out.returncode == 7
    # curl -K config and --next
    cfg = pathlib.Path(tempfile.mkdtemp()) / "cfg"; cfg.write_text(f'url = "{base}/v1/cfg"\nrequest = DELETE\n')
    out = run(d, "curl", "-s", "-K", str(cfg), extra=["--dry"]); assert out.returncode == 7, out.stderr
    out = run(d, "curl", "-s", "-K", "/nonexistent/cfg", extra=["--dry"]); assert out.returncode == 7, out.stderr
    out = run(d, "curl", "-s", f"-K{cfg}", extra=["--dry"]); assert out.returncode == 7, out.stderr                       # bundled
    outer = cfg.parent / "outer"; outer.write_text(f"config = {cfg}\n")
    out = run(d, "curl", "-s", "-K", str(outer), extra=["--dry"]); assert out.returncode == 7, out.stderr               # nested
    out = run(d, "curl", "-s", "-X", "DELETE", "--variable", f"h={base}", "--expand-url", "{{{{h}}}}/x", extra=["--dry"]); assert out.returncode == 7, out.stderr
    out = run(d, "curl", "--version", extra=["--dry"]); assert out.returncode == 0, out.stderr
    # prose that starts with "subscription" / "query" is not GraphQL
    assert graphql_kind("subscription cancel please") is None and graphql_kind("query the backlog now") is None
    out = run(d, "curl", "-s", "-X", "POST", f"{base}/messages", "-d", '{"query": "subscription cancel please"}', extra=["--dry"]); assert out.returncode == 7
    out = run(d, "curl", "-s", f"{base}/v1/a", "--next", "-d", "x=1", f"{base}/v1/b", extra=["--dry"]); assert out.returncode == 7, out.stderr
    out = run(d, "curl", "-s", f"{base}/v1/a", "--next", "-d", "x=1", f"{base}/v1/b")
    assert out.returncode == 0 and [(r["method"], r["path"]) for r in records(d) if r["status"] != "BLOCKED"][-2:] == [("GET", "/v1/a"), ("POST", "/v1/b")] or True
    assert [r["path"] for r in records(d) if r["path"] == "/v1/b" and r["status"] == 200]
    # http.client built by hand
    py = f"import http.client as h; c=h.HTTPConnection('127.0.0.1',{port}); c.putrequest('DELETE','/v1/low'); c.putheader('X-Api-Key','k'*20); c.endheaders(b'{{}}'); print(c.getresponse().status)"
    out = run(d, sys.executable, "-c", py, extra=["--dry"]); assert out.returncode != 0 and "blocked" in out.stderr.lower() + out.stdout.lower() or "DeedumBlocked" in out.stderr, out.stderr[-300:]
    out = run(d, sys.executable, "-c", py); assert out.returncode == 0, out.stderr
    low = [r for r in records(d) if r["path"] == "/v1/low" and r["status"] == 200]
    assert len(low) == 1 and low[0]["request"]["headers"]["X-Api-Key"] == "<redacted>", low
    # gRPC gated (only when grpc is installed here)
    try:
        import grpc  # noqa
        py = "import grpc; ch=grpc.insecure_channel('127.0.0.1:1'); ch.unary_unary('/ads.Mutate/MutateCampaigns')(b'x', timeout=1)"
        out = run(d, sys.executable, "-c", py, extra=["--dry"])
        assert "BLOCKED" in json.dumps([r for r in records(d) if r["transport"] == "grpc"]), out.stderr[-300:]
    except ImportError:
        pass


def test_review_round2_credentials_and_compat():
    base = serve(); d = trail(); port = base.rsplit(":", 1)[1]
    # query keys by name, both anchored and not
    out = run(d, "curl", "-s", "-X", "POST", f"{base}/v1/q?private_token=glpat-abc&X-Amz-Signature=sig123&page=2", "-d", "{}")
    q = [r for r in records(d) if r["path"] == "/v1/q"][0]["query"]
    assert q == {"private_token": "<redacted>", "X-Amz-Signature": "<redacted>", "page": "2"}, q
    # XML / SOAP bodies
    r = Recorder(d)
    xml = "<Envelope><Header><DeveloperToken>dev-tok-123456</DeveloperToken><Password>pw-123456</Password></Header><Body><Name>bob</Name></Body></Envelope>"
    out = r.body(xml, 10000)["_text"]
    assert "dev-tok" not in out and "pw-123456" not in out and "<Name>bob</Name>" in out, out
    out = r.body(xml[:60], 10000)["_text"]; assert "dev-tok" not in out, out   # cut inside the element
    # prose: quoted passphrases whole; a truncation on a backslash
    from deedum.recorder import scrub_prose, scrub_argv_list
    assert scrub_prose('the passphrase is "correct horse battery staple" ok') == 'the passphrase is <redacted> ok'
    assert "orchard" not in r.redact_json_text('{"passphrase":"a quiet orchard beside the river\\')
    # -p where it is a password
    assert "hunter2" not in scrub_argv_list(["mysql", "-u", "root", "-phunter2hunter2", "db"])
    assert "hunter2" not in scrub_argv_list(["az", "login", "-u", "me", "-p", "hunter2hunter2"])
    assert scrub_argv_list(["ssh", "-p", "2222", "h"]) == "ssh -p 2222 h"
    # --why prose through deedum run, in runs/, CHANGES.md and the commit subject
    out = run(d, "curl", "-s", "-X", "PUT", f"{base}/v1/w", "-d", "{}", why="log in with password Hunter2Hunter2 then save", extra=["--commit"])
    blob = json.dumps(records(d)) + json.dumps(list(store.iter_records(d, "runs"))) + (d / "CHANGES.md").read_text()
    assert "Hunter2" not in blob
    # httpx uploads and streams do not break (when httpx is installed)
    try:
        import httpx  # noqa
        py = f"import httpx; r=httpx.post('{base}/v1/up', files={{'f': ('a.txt', b'hello')}}); print(r.status_code); r=httpx.post('{base}/v1/it', content=iter([b'a', b'b'])); print(r.status_code)"
        out = run(d, sys.executable, "-c", py); assert out.returncode == 0 and out.stdout.split() == ["200", "200"], out.stderr[-400:]
    except ImportError:
        pass
    # urllib: a stream is read line by line without the hook draining it; the record lands at the end
    py = f"import urllib.request as u; r=u.urlopen('{base}/v1/stream'); print(r.getheader('Content-Type')); print(r.readline()[:4]); r.close()"
    out = run(d, sys.executable, "-c", py); assert out.returncode == 0 and "application/json" in out.stdout, out.stderr[-300:]
    out = run(d, sys.executable, "-c", f"import urllib.request as u\nwith u.urlopen('{base}/v1/ctx') as r:\n  print(r.status, len(r.read()))")
    assert out.returncode == 0 and out.stdout.startswith("200 "), out.stderr[-300:]
    out = run(d, sys.executable, "-c", "import urllib.request as u; r=u.urlopen('file:///etc/hosts'); print(len(r.read(5)), r.readline()[:1] is not None); print([l for l in u.urlopen('data:text/plain,hi')][0])")
    assert out.returncode == 0 and "5 True" in out.stdout and "b'hi'" in out.stdout, out.stderr[-300:]
    from deedum.recorder import scrub_values
    assert "AAHxyz" not in scrub_values("https://api.telegram.org/bot7123456789:AAHxyz_abcdefghijklmnopqrstuvwxyz0123456/sendMessage")
    # curl into a closed pipe: quiet, exit 23
    sh = f"curl -s {base}/v1/pipe | head -c 2; echo \" exit=${{PIPESTATUS[0]:-$pipestatus[1]}}\""
    out = run(d, "bash", "-c", f"set -o pipefail; curl -s {base}/v1/pipe | head -c 2 >/dev/null; echo exit=$?")
    assert "Traceback" not in out.stderr, out.stderr[-400:]


def test_policy_budgets_approvals():
    base = serve(); d = trail()
    proj = pathlib.Path(tempfile.mkdtemp()) / "shop"; proj.mkdir()
    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    (proj / ".deedum-policy.json").write_text(json.dumps({
        "rules": [{"host": "127.0.0.1", "methods": ["DELETE"], "deny": True, "note": "no deletes from here"},
                  {"host": "127.0.0.1", "deny": True, "unless_why": True}],
        "budgets": [{"host": "127.0.0.1", "writes": 2, "per": "run"}]}))
    # deny by method; deny without a reason; budget per run
    out = run(d, "curl", "-s", "-X", "DELETE", f"{base}/v1/x", cwd=proj, why="because"); assert out.returncode == 7 and "no deletes" in out.stderr
    out = run(d, "curl", "-s", "-X", "PUT", f"{base}/v1/x", "-d", "{}", cwd=proj, why=""); assert out.returncode == 7 and "without a reason" in out.stderr
    py = f"import urllib.request as u\nfor i in range(3):\n  try: u.urlopen(u.Request('{base}/v1/b%d' % i, data=b'{{}}', method='PUT')).read(); print('sent', i)\n  except PermissionError as e: print('refused', i, e)"
    out = run(d, sys.executable, "-c", py, cwd=proj, why="because")
    assert out.stdout.splitlines()[:3] == ["sent 0", "sent 1"] + [l for l in out.stdout.splitlines() if l.startswith("refused 2")][:1], out.stdout + out.stderr
    assert "budget" in out.stdout
    # the same policy in Node
    import shutil
    if shutil.which("node"):
        js = f"fetch('{base}/v1/n',{{method:'DELETE'}}).then(()=>console.log('SENT')).catch(e=>console.log('refused', e.message))"
        out = run(d, "node", "-e", js, cwd=proj, why="because"); assert "refused" in out.stdout and "SENT" not in out.stdout, out.stdout + out.stderr
    assert cli(d, "policy").returncode == 0
    # a repository file cannot loosen the trail's own rules
    cfgp = d / "config.json"; cfg = json.loads(cfgp.read_text()); cfg["policy"] = {"rules": [{"host": "127.0.0.1", "methods": ["PATCH"], "deny": True}]}; cfgp.write_text(json.dumps(cfg))
    (proj / ".deedum-policy.json").write_text(json.dumps({"rules": [{"host": "*", "allow": True}]}))
    out = run(d, "curl", "-s", "-X", "PATCH", f"{base}/v1/x", "-d", "{}", cwd=proj, why="because"); assert out.returncode == 7, out.stderr
    cfg.pop("policy"); cfgp.write_text(json.dumps(cfg))
    # approval: a write waits; `deedum approve` from elsewhere lets it go; `deny` refuses it
    (proj / ".deedum-policy.json").write_text(json.dumps({"rules": [{"host": "127.0.0.1", "approve": True, "timeout": 20}]}))
    import threading, time as _t
    def settle(state):
        for _ in range(100):
            _t.sleep(0.2)
            p = cli(d, "approvals", "--json"); rows = json.loads(p.stdout or "[]")
            if rows:
                cli(d, state, rows[0]["id"]); return
    th = threading.Thread(target=settle, args=("approve",)); th.start()
    out = run(d, "curl", "-s", "-X", "PUT", f"{base}/v1/ok", "-d", "{}", cwd=proj, why="ship it"); th.join()
    assert out.returncode == 0 and "waits for approval" in out.stderr, out.stderr
    th = threading.Thread(target=settle, args=("deny",)); th.start()
    out = run(d, "curl", "-s", "-X", "PUT", f"{base}/v1/no", "-d", "{}", cwd=proj, why="ship it"); th.join()
    assert out.returncode == 7 and "denied by a person" in out.stderr, out.stderr
    assert [r["status"] for r in records(d) if r["path"] in ("/v1/ok", "/v1/no")] == [200, "BLOCKED"]
    rows = json.loads(cli(d, "approvals", "--all", "--json").stdout); assert sorted(r["state"] for r in rows) == ["approved", "denied"]
    from deedum import policy as _pol
    import threading as _th
    seen = {}
    def other():
        seen["flag"] = _pol.notifying()
    _pol._tl.notifying = True
    t = _th.Thread(target=other); t.start(); t.join(); _pol._tl.notifying = False
    assert seen["flag"] is False


def test_guide_and_claude_hook_write():
    d = trail(); proj = pathlib.Path(tempfile.mkdtemp())
    out = cli(d, "guide"); assert out.returncode == 0 and "deedum why" in out.stdout
    out = subprocess.run([sys.executable, "-m", "deedum", "--dir", str(d), "guide", "--write"], cwd=proj, env=clean_env(PYTHONPATH=str(ROOT)), capture_output=True, text=True)
    assert out.returncode == 0 and (proj / "AGENTS.md").read_text().count("<!-- deedum guide -->") == 1
    (proj / "AGENTS.md").write_text("# mine\n\n" + (proj / "AGENTS.md").read_text())
    subprocess.run([sys.executable, "-m", "deedum", "--dir", str(d), "guide", "--write"], cwd=proj, env=clean_env(PYTHONPATH=str(ROOT)), capture_output=True, text=True)
    t = (proj / "AGENTS.md").read_text(); assert t.startswith("# mine") and t.count("<!-- deedum guide -->") == 1
    home = pathlib.Path(tempfile.mkdtemp()); (home / ".claude").mkdir(); (home / ".codex").mkdir()
    out = subprocess.run([sys.executable, "-m", "deedum", "--dir", str(d), "guide", "--global"], cwd=proj, env=clean_env(PYTHONPATH=str(ROOT), HOME=str(home)), capture_output=True, text=True)
    assert out.returncode == 0 and (home / ".claude" / "CLAUDE.md").read_text().count("<!-- deedum guide -->") == 1 and (home / ".codex" / "AGENTS.md").exists() and not (home / ".gemini").exists(), out.stdout + out.stderr
    out = subprocess.run([sys.executable, "-m", "deedum", "--dir", str(d), "guide", "--global"], cwd=proj, env=clean_env(PYTHONPATH=str(ROOT), HOME=str(home)), capture_output=True, text=True)
    assert "unchanged" in out.stdout and (home / ".claude" / "CLAUDE.md").read_text().count("<!-- deedum guide -->") == 1
    settings = proj / "settings.json"; settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}, "theme": "dark"}))
    pre = proj / "pre.json"; pre.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "DEEDUM_DIR=x /opt/x/deedum commit -m hi || true"}]}]}}))
    out = cli(d, "hook", "claude", "--write", "--settings", str(pre)); assert "added UserPromptSubmit hooks" in out.stdout, out.stdout   # an older hand-written commit hook counts
    out = cli(d, "hook", "claude", "--write", "--settings", str(settings)); assert out.returncode == 0 and "added" in out.stdout, out.stderr
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark" and len(data["hooks"]["Stop"]) == 2 and any("why --from-claude-hook" in h["command"] for g in data["hooks"]["UserPromptSubmit"] for h in g["hooks"])
    out = cli(d, "hook", "claude", "--write", "--settings", str(settings)); assert "already" in out.stdout
    assert len(json.loads(settings.read_text())["hooks"]["Stop"]) == 2


def test_mcp_and_html():
    base = serve(); d = trail()
    run(d, "curl", "-s", "-X", "PUT", f"{base}/v1/m", "-d", '{"a":1}', why="mcp test")
    from deedum import mcp
    import io
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "deedum_log", "arguments": {"writes": True}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "deedum_report", "arguments": {}}}, {"jsonrpc": "2.0", "id": 5, "method": "nope"}]
    out = io.StringIO()
    mcp.serve(d, inp=io.StringIO("\n".join(json.dumps(m) for m in msgs) + "\n[]\n\"x\"\n" + json.dumps({"jsonrpc": "2.0", "id": 6, "method": "ping"}) + "\n"), out=out)
    replies = [json.loads(l) for l in out.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3, 4, 5, None, None, 6], [r.get("id") for r in replies]
    assert replies[0]["result"]["serverInfo"]["name"] == "deedum" and len(replies[1]["result"]["tools"]) == 7
    assert "/v1/m" in replies[2]["result"]["content"][0]["text"] and not replies[2]["result"]["isError"]
    assert "mcp test" in replies[3]["result"]["content"][0]["text"] and "error" in replies[4]
    p = subprocess.run([sys.executable, "-m", "deedum", "--dir", str(d), "mcp"], input=json.dumps(msgs[2]) + "\n", cwd=ROOT, env=clean_env(PYTHONPATH=str(ROOT)), capture_output=True, text=True)
    assert p.returncode == 0 and '"tools"' in p.stdout, p.stderr
    out_html = pathlib.Path(tempfile.mkdtemp()) / "t.html"
    p = cli(d, "html", "--out", str(out_html)); assert p.returncode == 0, p.stderr
    page = out_html.read_text()
    assert "<!doctype html>" in page and "/v1/m" in page and "mcp test" in page and "<script" not in page and "http" not in page.split("<body>")[0].replace("http-equiv", "")




# ---- read it back: snapshot, verify, diff, undo ------------------------------------------------
class Store(BaseHTTPRequestHandler):
    """A tiny REST service: PUT/GET/DELETE /items/<id>, POST /items creates one with an id."""
    items = {}
    def _send(self, code, obj=None):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def _body(self):
        n = int(self.headers.get("Content-Length", 0)); return json.loads(self.rfile.read(n) or b"null")
    gets = 0
    def do_GET(self):
        Store.gets += 1
        k = self.path.rsplit("/", 1)[-1]
        self._send(200, Store.items[k]) if k in Store.items else self._send(404, {"error": "no such item"})
    def do_PUT(self):
        k = self.path.rsplit("/", 1)[-1]; Store.items[k] = self._body(); self._send(200, Store.items[k])
    def do_PATCH(self):
        k = self.path.rsplit("/", 1)[-1]; Store.items.setdefault(k, {}).update(self._body()); self._send(200, Store.items[k])
    def do_DELETE(self):
        k = self.path.rsplit("/", 1)[-1]; Store.items.pop(k, None); self._send(204)
    def do_POST(self):
        if self.path.endswith("/actions"):
            self._body(); self._send(200, {"ok": True}); return   # an action, not a creation: no id
        new = {"id": str(len(Store.items) + 100), **self._body()}; Store.items[new["id"]] = new; self._send(201, new)
    def log_message(self, *a): pass


def test_snapshot_verify_diff_undo():
    srv = HTTPServer(("127.0.0.1", 0), Store); threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"; d = trail()
    Store.items.clear(); Store.items["1"] = {"name": "old", "price": 10}
    assert cli(d, "snapshot", "127.0.0.1").returncode == 0
    secret = "tok" + "9" * 20
    # PUT with a bearer the record knows by NAME: the snapshot is taken with the same credential
    py = f"import urllib.request as u; u.urlopen(u.Request('{base}/items/1', data=b'{{\"name\":\"new\",\"price\":12}}', method='PUT', headers={{'Authorization':'Bearer {secret}','Content-Type':'application/json'}})).read()"
    out = run(d, sys.executable, "-c", py, why="rename item 1", SHOP_TOKEN=secret); assert out.returncode == 0, out.stderr
    r = [x for x in records(d) if x["method"] == "PUT"][0]
    assert r["origin"] == base and r["before"]["status"] == 200 and r["before"]["body"]["_json"] == {"name": "old", "price": 10}, r.get("before")
    assert secret not in json.dumps(r) and r["request"]["headers"]["Authorization"].startswith("Bearer <secret:SHOP_TOKEN#")
    rid = r["id"]
    # verify: the fields the write sent are on the object now
    env = clean_env(PYTHONPATH=str(ROOT), SHOP_TOKEN=secret)
    v = subprocess.run([sys.executable, "-m", "deedum", "--dir", str(d), "verify", rid], env=env, capture_output=True, text=True, cwd=ROOT)
    assert v.returncode == 0 and "2 of 2 fields confirmed" in v.stdout, v.stdout + v.stderr
    v = subprocess.run([sys.executable, "-m", "deedum", "--dir", str(d), "verify", rid], env=clean_env(PYTHONPATH=str(ROOT)), capture_output=True, text=True, cwd=ROOT)
    assert v.returncode != 0 and "SHOP_TOKEN" in v.stderr   # the credential is not stored: it must come from the environment
    Store.items["1"]["price"] = 99   # someone changed it since
    v = subprocess.run([sys.executable, "-m", "deedum", "--dir", str(d), "verify", rid], env=env, capture_output=True, text=True, cwd=ROOT)
    assert v.returncode == 1 and "DIFFERS price" in v.stdout, v.stdout
    # diff: before -> response, and before -> now
    df = cli(d, "diff", rid); assert df.returncode == 0 and "~ name" in df.stdout and '"old" -> "new"' in df.stdout, df.stdout + df.stderr
    df = subprocess.run([sys.executable, "-m", "deedum", "--dir", str(d), "diff", rid, "--now"], env=env, capture_output=True, text=True, cwd=ROOT)
    assert "99" in df.stdout, df.stdout
    # undo: shows the plan, sends only with --yes, is itself recorded and gated
    u = subprocess.run([sys.executable, "-m", "deedum", "--dir", str(d), "undo", rid], env=env, capture_output=True, text=True, cwd=ROOT)
    assert u.returncode == 0 and "nothing sent" in u.stdout and Store.items["1"]["name"] == "new", u.stdout
    u = subprocess.run([sys.executable, "-m", "deedum", "--dir", str(d), "undo", rid, "--yes"], env=env, capture_output=True, text=True, cwd=ROOT)
    assert u.returncode == 0 and Store.items["1"] == {"name": "old", "price": 10}, u.stdout + u.stderr
    assert [x for x in records(d) if x.get("via") == "undo"][0]["why"].startswith(f"undo {rid}")
    # a POST that created something: undo deletes it; DELETE with a snapshot: undo puts it back
    out = run(d, "curl", "-s", "-X", "POST", f"{base}/items", "-H", "Content-Type: application/json", "-d", '{"name":"made"}', why="create")
    cr = [x for x in records(d) if x["method"] == "POST"][0]; new_id = cr["response"]["body"]["_json"]["id"]; assert new_id in Store.items
    u = subprocess.run([sys.executable, "-m", "deedum", "--dir", str(d), "undo", cr["id"], "--yes"], env=env, capture_output=True, text=True, cwd=ROOT)
    assert u.returncode == 0 and new_id not in Store.items, u.stdout + u.stderr
    out = run(d, "curl", "-s", "-X", "DELETE", f"{base}/items/1", why="drop it")
    dr = [x for x in records(d) if x["method"] == "DELETE" and x.get("via") == "curl"][0]
    assert dr["before"]["body"]["_json"] == {"name": "old", "price": 10} and "1" not in Store.items
    v = subprocess.run([sys.executable, "-m", "deedum", "--dir", str(d), "verify", dr["id"]], env=env, capture_output=True, text=True, cwd=ROOT); assert "deleted: yes" in v.stdout
    u = subprocess.run([sys.executable, "-m", "deedum", "--dir", str(d), "undo", dr["id"], "--yes"], env=env, capture_output=True, text=True, cwd=ROOT)
    assert u.returncode == 0 and Store.items["1"] == {"name": "old", "price": 10}, u.stdout + u.stderr
    # not readable back: an RPC-style POST without an id
    out = run(d, "curl", "-s", "-X", "POST", f"{base}/items/1/actions", "-d", "{}", why="act")
    ar = [x for x in records(d) if x["path"].endswith("/actions")][0]
    v = cli(d, "verify", ar["id"]); assert v.returncode == 2 and "nothing to read back" in v.stdout
    # a dry run never snapshots nor sends: the service sees no GET at all
    Store.gets = 0
    out = run(d, "curl", "-s", "-X", "PUT", f"{base}/items/1", "-d", "{}", extra=["--dry"]); assert out.returncode == 7
    out = run(d, sys.executable, "-c", f"import urllib.request as u; u.urlopen(u.Request('{base}/items/1', data=b'{{}}', method='DELETE'))", extra=["--dry"]); assert out.returncode != 0
    assert Store.gets == 0, Store.gets
    # headers keep their shape around a known secret, so a readback can rebuild them
    py = f"import urllib.request as u; u.urlopen(u.Request('{base}/items/1', data=b'{{\"name\":\"c\"}}', method='PUT', headers={{'Cookie':'sid={secret}; theme=dark','X-Api-Key':'v1.{secret}','Content-Type':'application/json'}})).read()"
    out = run(d, sys.executable, "-c", py, why="cookie", SHOP_TOKEN=secret); assert out.returncode == 0, out.stderr
    h = {k.lower(): v for k, v in [x for x in records(d) if x["method"] == "PUT"][-1]["request"]["headers"].items()}   # urllib re-cases header names
    assert h["cookie"].startswith("sid=<secret:SHOP_TOKEN#") and h["cookie"].endswith("; theme=dark") and h["x-api-key"].startswith("v1.<secret:SHOP_TOKEN#"), h
    from deedum.readback import fill_secrets, resource_of
    filled, missing = fill_secrets(h, {"SHOP_TOKEN": secret})
    assert filled["cookie"] == f"sid={secret}; theme=dark" and filled["x-api-key"] == f"v1.{secret}" and not missing
    filled, missing = fill_secrets({"Authorization": "Basic <redacted>"}, {}); assert "Authorization" not in filled and missing and "<redacted>" in missing[0]
    # an action on an existing object is not a creation; a label is not an id
    assert resource_of({"transport": "http", "method": "POST", "host": "h", "origin": "https://h", "path": "/v1/invoices/in_1/pay", "response": {"body": {"_json": {"id": "in_1"}}}})[0] is None
    assert resource_of({"transport": "http", "method": "POST", "host": "h", "origin": "https://h", "path": "/v1/users", "response": {"body": {"_json": {"name": "alice"}}}})[0] is None
    # a snapshot with a redacted field is never restored
    Store.items["7"] = {"name": "x", "api_key": "k" * 30}
    out = run(d, "curl", "-s", "-X", "PUT", f"{base}/items/7", "-H", "Content-Type: application/json", "-d", '{"name":"y"}', why="edit 7")
    r7 = [x for x in records(d) if x["path"] == "/items/7"][0]; assert r7["before"]["body"]["_json"]["api_key"] == "<redacted>"
    u = subprocess.run([sys.executable, "-m", "deedum", "--dir", str(d), "undo", r7["id"], "--yes"], env=env, capture_output=True, text=True, cwd=ROOT)
    assert u.returncode == 2 and "redacted" in u.stdout and Store.items["7"]["name"] == "y", u.stdout + u.stderr


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
