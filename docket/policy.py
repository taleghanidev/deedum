"""Policy: what a call from here may do, decided BEFORE it leaves. Shared by the Python hooks,
the curl and exec shims (and mirrored in the Node hook).

Where it comes from: `policy` in the trail's config.json first (a repository cannot loosen it),
then the nearest `.docket-policy.json` walking up from the working directory (a repo's own
rules, in git with the code). The first rule that matches decides; no match means allowed. Budgets count writes per session
or per run and refuse the one that would go past. A write to an `approve` host waits for
`docket approve ID` (or refuses after `timeout` seconds).

{
  "rules": [
    {"host": "api.stripe.com", "deny": true, "unless_why": true},   # no reason, no Stripe write
    {"host": "*.googleapis.com", "methods": ["DELETE"], "deny": true},
    {"host": "api.cloudflare.com", "approve": true, "timeout": 600},
    {"host": "*", "project": "scratch", "deny": true}                # a scratch repo changes nothing
  ],
  "budgets": [
    {"host": "api.stripe.com", "writes": 20, "per": "session"},
    {"host": "*", "writes": 500, "per": "run"}
  ],
  "notify": "https://api.telegram.org/bot<secret:TG_BOT_TOKEN>/sendMessage?chat_id=1"   # optional: where approvals are announced
}
"""
import os, json, time, pathlib, fnmatch, uuid


def find_policy_file(cwd=None):
    d = pathlib.Path(cwd or os.getcwd()).resolve()
    home = pathlib.Path.home().resolve()
    for p in [d, *d.parents]:
        f = p / ".docket-policy.json"
        if f.is_file():
            return f
        if p == home:
            break
    return None


def load(cfg, cwd=None):
    """The policy in force: the trail's config `policy` first (it cannot be loosened from a repository),
    then the nearest .docket-policy.json."""
    pol = dict((cfg or {}).get("policy") or {})
    f = find_policy_file(cwd)
    if f:
        try:
            local = json.loads(f.read_text())
            # the trail's own rules come FIRST: a repository file cannot allow what the trail denies
            pol = {**pol, **local, "rules": list(pol.get("rules") or []) + list(local.get("rules") or []),
                   "budgets": list(pol.get("budgets") or []) + list(local.get("budgets") or [])}
            pol["_file"] = str(f)
        except (OSError, ValueError):
            pass
    return pol


def _match(rule, host, method, project, kind):
    if not fnmatch.fnmatch((host or "").lower(), (rule.get("host") or "*").lower()):
        return False
    if rule.get("methods") and method.upper() not in [m.upper() for m in rule["methods"]]:
        return False
    if rule.get("project") and not fnmatch.fnmatch(project or "", rule["project"]):
        return False
    if rule.get("kind", "write") != "any" and rule.get("kind", "write") != kind:
        return False
    return True


def decide(pol, trail, host, method, kind, project, why, run_id, session):
    """None to let the call go, a reason string to refuse it, or ("approve", rule) to hold it."""
    for rule in pol.get("rules") or []:
        if not _match(rule, host, method, project, kind):
            continue
        if rule.get("deny"):
            if rule.get("unless_why") and why:
                continue
            return f"docket policy: {method} {host} is refused" + (" without a reason (docket why \"...\")" if rule.get("unless_why") else "") \
                + (f" ({rule.get('note')})" if rule.get("note") else "")
        if rule.get("approve"):
            return ("approve", rule)
        if rule.get("allow"):
            return None
    if kind == "write":
        for b in pol.get("budgets") or []:
            if not fnmatch.fnmatch((host or "").lower(), (b.get("host") or "*").lower()):
                continue
            if b.get("project") and not fnmatch.fnmatch(project or "", b["project"]):
                continue
            per = b.get("per", "session")
            key = run_id if per == "run" else (session or "default")
            n = count_writes(trail, host_pat=b.get("host") or "*", per=per, key=key)
            if n >= int(b.get("writes", 0)):
                return f"docket budget: {n} writes to {b.get('host') or 'any host'} already this {per} (limit {b.get('writes')})"
    return None


def count_writes(trail, host_pat, per, key):
    """Writes already recorded for this session or run to hosts matching the pattern. Reads
    today's and yesterday's day files only: a session is at most 12 hours of reason."""
    from . import store
    import datetime
    n = 0
    today = datetime.date.today()
    since = (today - datetime.timedelta(days=1)).isoformat()
    field = "run" if per == "run" else "run"
    for r in store.iter_records(trail, "calls", since=since):
        if r.get("kind") != "write" or r.get("status") == "BLOCKED":
            continue
        if not fnmatch.fnmatch((r.get("host") or "").lower(), host_pat.lower()):
            continue
        if per == "run":
            if r.get("run") == key:
                n += 1
        else:
            sess = (r.get("agent") or {}).get("session") or (r.get("run") if str(r.get("run", "")).startswith("session-") else None)
            if sess and key and (sess == key or str(r.get("run", "")) == f"session-{key[:8]}"):
                n += 1
            elif not sess and key == "default":
                n += 1
    return n


# ---- approvals: a write waits for a person -----------------------------------------------------
def request_approval(trail, rule, host, method, path, project, why, agent, notify=None):
    """Write the request, announce it, wait for `docket approve ID` / `docket deny ID`. Returns
    None when approved, a refusal string otherwise."""
    d = pathlib.Path(trail) / ".state" / "approvals"
    d.mkdir(parents=True, exist_ok=True)
    aid = uuid.uuid4().hex[:8]
    f = d / f"{aid}.json"
    req = {"id": aid, "host": host, "method": method, "path": path, "project": project, "why": why, "agent": agent,
           "asked": time.time(), "state": "pending"}
    f.write_text(json.dumps(req))
    timeout = int(rule.get("timeout") or 600)
    line = f"[docket] {method} {host}{path} from {project or '?'} ({why or 'no reason'}) waits for approval: docket approve {aid}   (deny: docket deny {aid}; {timeout}s)"
    try:
        import sys
        sys.stderr.write(line + "\n")
    except Exception:
        pass
    if notify:
        _notify(notify, line)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            state = json.loads(f.read_text()).get("state")
        except (OSError, ValueError):
            state = "pending"
        if state == "approved":
            return None
        if state == "denied":
            return f"docket: {method} {host}{path} denied by a person (approval {aid})"
        time.sleep(1)
    try:
        f.write_text(json.dumps({**req, "state": "expired"}))
    except OSError:
        pass
    return f"docket: {method} {host}{path} not approved within {timeout}s (approval {aid})"


import threading
_tl = threading.local()


def notifying():
    """True in the thread that is sending an announcement right now: that one call is not policed
    (or a deny-all rule would stop its own announcement). Other threads are unaffected."""
    return getattr(_tl, "notifying", False)


def _notify(url, text):
    """POST the line to the notify URL (a webhook; `<secret:NAME>` in it is filled from the
    environment). Never raises. The call is recorded like any other, but not policed, or a
    deny-all rule would stop its own announcement."""
    try:
        import re, urllib.request
        url = re.sub(r"<secret:([A-Za-z_][A-Za-z0-9_]*)(#[0-9a-f]+)?>", lambda m: os.environ.get(m.group(1), ""), url)
        body = json.dumps({"text": text}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        _tl.notifying = True
        try:
            urllib.request.urlopen(req, timeout=10).read()
        finally:
            _tl.notifying = False
    except Exception:
        pass


def list_approvals(trail, pending_only=True):
    d = pathlib.Path(trail) / ".state" / "approvals"
    out = []
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            r = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if not pending_only or r.get("state") == "pending":
            out.append(r)
    return out


def settle(trail, aid, state):
    f = pathlib.Path(trail) / ".state" / "approvals" / f"{aid}.json"
    try:
        r = json.loads(f.read_text())
    except (OSError, ValueError):
        return None
    if r.get("state") != "pending":
        return r
    r["state"] = state
    r["settled"] = time.time()
    f.write_text(json.dumps(r))
    return r
