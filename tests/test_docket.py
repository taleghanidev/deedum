"""Run: python3 -m pytest tests  (or: python3 tests/test_docket.py)"""
import json, os, subprocess, sys, tempfile, threading, pathlib
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from docket import store
from docket.recorder import Recorder

class Echo(BaseHTTPRequestHandler):
    def _r(self):
        n = int(self.headers.get("Content-Length", 0)); b = self.rfile.read(n)
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(b'{"ok":true,"echo":' + (b or b"null") + b"}")
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
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DOCKET_", "CLAUDE")) and k not in ("PYTHONPATH", "NODE_OPTIONS")}
    env.update(kw)
    return env


def run(d, *cmd, why="t", extra=(), cwd=ROOT, **envkw):
    env = clean_env(DOCKET_DIR=str(d), PYTHONPATH=str(ROOT), **envkw)
    w = ["--why", why] if why else []
    return subprocess.run([sys.executable, "-m", "docket", "run", *w, *extra, "--", *cmd],
                          cwd=cwd, env=env, capture_output=True, text=True)


def cli(d, *a):
    return subprocess.run([sys.executable, "-m", "docket", "--dir", str(d), *a], cwd=ROOT,
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
    from docket.recorder import scrub_values
    os.environ["DOCKET_TEST_API_TOKEN"] = "OddFormatValue0123456789"
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
    env = clean_env(PYTHONPATH=str(ROOT), DOCKET_SESSION="s1")
    hook = subprocess.run([sys.executable, "-m", "docket", "--dir", str(d), "why", "--from-claude-hook"],
                          input=json.dumps({"session_id": "s1", "prompt": "raise   the bids\nplease"}),
                          env=env, capture_output=True, text=True, cwd=ROOT)
    assert hook.returncode == 0 and hook.stdout == "", hook
    out = run(d, "curl", "-s", "-X", "PUT", f"{base}/v1/b", "-d", "{}", why="", DOCKET_SESSION="s1")
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
    out = cli(d, "compact", "--older", "1d", "--strip-read-bodies")
    assert out.returncode == 0, out.stderr
    assert (d / "calls" / "2020-01-01.jsonl.gz").exists()
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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
