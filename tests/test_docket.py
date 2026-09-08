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
    cfg = json.loads((d / "config.json").read_text()); cfg["ignore_hosts"] = []
    (d / "config.json").write_text(json.dumps(cfg))
    return d

def run(d, *cmd, why="t"):
    env = dict(os.environ, DOCKET_DIR=str(d))
    return subprocess.run([sys.executable, "-m", "docket", "run", "--why", why, "--", *cmd],
                          cwd=ROOT, env=env, capture_output=True, text=True)

def records(d):
    return list(store.iter_records(d))

def test_redaction():
    r = Recorder(trail())
    assert r.body("a=1&refresh_token=S&b=2", 999)["_text"] == "a=1&refresh_token=<redacted>&b=2"
    assert r.body('{"access_token":"S","x":{"client_secret":"S"}}', 999)["_json"] == {"access_token": "<redacted>", "x": {"client_secret": "<redacted>"}}
    assert r.redact_headers({"Authorization": "Bearer S", "Accept": "*/*"}) == {"Authorization": "<redacted>", "Accept": "*/*"}
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
    assert recs[0]["request"]["headers"]["Authorization"] == "<redacted>"
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

if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
