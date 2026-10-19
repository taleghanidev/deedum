"""Read a changed object back from the service it lives in: the `before` snapshot taken just
ahead of a write, `deedum verify ID` after it, `deedum diff ID` between the two, and
`deedum undo ID`, which puts the `before` back.

What it can do is bounded by what REST makes guessable. A write to `/v1/items/42` (PUT, PATCH,
DELETE) is a change to the object at that URL: it can be read with GET before and after, and put
back with PUT. A POST to `/v1/items` that answers with an `id` created `/v1/items/<id>`, which
can be read back and deleted. Anything else (an RPC, a GraphQL mutation, a POST that is an
action) is reported as "cannot read back", never guessed.

Credentials are never stored, so a readback uses the record's credential NAMES: a header stored
as `Bearer <secret:STRIPE_SECRET_KEY#a1b2>` is rebuilt from `$STRIPE_SECRET_KEY` in the
environment of the person running the command. Missing from the environment: the readback says
so and stops.
"""
import os, re, json, http.client, ssl, urllib.parse

_SECRET_TAG = re.compile(r"<secret:([A-Za-z_][A-Za-z0-9_]*)(?:#[0-9a-f]+)?>")
# Reads of these are the write's own kind of object; the id is the last path segment.
_ID_RE = re.compile(r"^[A-Za-z0-9_.:@=-]{1,128}$")
# Headers that are part of the request's identity, not of a GET (length, encoding, method overrides).
_SKIP_H = {"content-length", "content-type", "transfer-encoding", "x-http-method-override", "host", "connection", "accept-encoding"}


def fill_secrets(headers, env=None):
    """The recorded headers with <secret:NAME> filled from the environment. Returns
    (headers, missing): missing names a variable that is not set, or "<redacted>" for a header
    whose credential was never known by name and so cannot be rebuilt at all."""
    env = env if env is not None else os.environ
    out, missing = {}, []
    for k, v in (headers or {}).items():
        if k.lower() in _SKIP_H:
            continue
        v = str(v)
        if "<redacted>" in v:
            missing.append(f"<redacted> ({k}: the credential was not known by name when recorded)")
            continue
        def sub(m):
            val = env.get(m.group(1))
            if val is None:
                missing.append(m.group(1))
                return ""
            return val
        out[k] = _SECRET_TAG.sub(sub, v)
    return out, sorted(set(missing))


def resource_of(record):
    """(kind, url) for the object the record changed: kind is "object" (PUT/PATCH/DELETE on an
    item URL), "created" (POST that answered with an id), or None with a reason string."""
    if record.get("transport") != "http":
        return None, f"not an HTTP call ({record.get('transport')}): nothing to read back"
    method, host, path = (record.get("method") or "").upper(), record.get("host") or "", record.get("path") or "/"
    origin = record.get("origin") or ("http://" + host if host in ("localhost", "127.0.0.1", "::1") else "https://" + host)
    base = origin.rstrip("/") + path
    if method in ("PUT", "PATCH", "DELETE"):
        seg = path.rstrip("/").rsplit("/", 1)[-1]
        if not seg or not _ID_RE.match(seg) or seg in ("batch", "bulk", "search", "query"):
            return None, f"{method} {path} does not name one object: nothing to read back"
        return "object", base
    if method == "POST":
        body = ((record.get("response") or {}).get("body") or {})
        j = body.get("_json") if isinstance(body, dict) else None
        new_id = None
        if isinstance(j, dict):
            for k in ("id", "ID", "Id", "uuid", "resource_name", "resourceName", "sid"):   # not "name": that is a label
                v = j.get(k)
                if isinstance(v, (str, int)) and _ID_RE.match(str(v)):
                    new_id = str(v); break
            if new_id is None and isinstance(j.get("data"), dict) and isinstance(j["data"].get("id"), (str, int)):
                new_id = str(j["data"]["id"])
        if new_id is None:
            return None, f"POST {path} answered without an id: nothing to read back"
        if new_id in path.split("/"):
            # `POST /v1/invoices/in_1/pay` -> {"id": "in_1"}: an action on an existing object,
            # not a creation; there is nothing to delete
            return None, f"POST {path} acted on {new_id}, which already existed: nothing to read back"
        if new_id.startswith(path.strip("/")):   # resource_name style: customers/1/campaigns/9
            return "created", f"{origin.rstrip('/')}/{new_id}"
        return "created", f"{base.rstrip('/')}/{new_id}"
    return None, f"{method} is a read: nothing to read back"


def http_call(method, url, headers=None, body=None, timeout=20):
    """One HTTP call with the standard library, outside the hooks (an Accept: application/json
    GET or a PUT/DELETE for undo). Returns (status, parsed body or text)."""
    u = urllib.parse.urlsplit(url)
    ctx = ssl.create_default_context()
    conn = (http.client.HTTPSConnection(u.hostname, u.port, timeout=timeout, context=ctx) if u.scheme == "https"
            else http.client.HTTPConnection(u.hostname, u.port, timeout=timeout))
    h = {"Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode() if not isinstance(body, (bytes, str)) else body
        h.setdefault("Content-Type", "application/json")
    conn.request(method, (u.path or "/") + (("?" + u.query) if u.query else ""), body=data, headers=h)
    r = conn.getresponse()
    raw = r.read()
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else None
    except (ValueError, UnicodeDecodeError):
        parsed = raw.decode("utf-8", "replace")[:20000]
    conn.close()
    return r.status, parsed


def request_fields(record):
    """The JSON fields the write sent (what a verify checks for)."""
    body = (record.get("request") or {}).get("body") or {}
    j = body.get("_json") if isinstance(body, dict) else None
    return j if isinstance(j, dict) else None


def compare(sent, got):
    """Which of the sent top-level fields the read-back object carries with the same value.
    Returns (matched, differing {field: (sent, got)}, absent [fields])."""
    if not isinstance(sent, dict) or not isinstance(got, dict):
        return [], {}, list(sent or {})
    matched, differing, absent = [], {}, []
    for k, v in sent.items():
        if k not in got:
            absent.append(k)
        elif got[k] == v or str(got[k]) == str(v):
            matched.append(k)
        else:
            differing[k] = (v, got[k])
    return matched, differing, absent


def diff(before, after, path=""):
    """Lines describing what changed between two JSON values."""
    out = []
    if isinstance(before, dict) and isinstance(after, dict):
        for k in sorted(set(before) | set(after)):
            p = f"{path}.{k}" if path else k
            if k not in before:
                out.append(f"+ {p} = {json.dumps(after[k], ensure_ascii=False)[:200]}")
            elif k not in after:
                out.append(f"- {p} = {json.dumps(before[k], ensure_ascii=False)[:200]}")
            else:
                out += diff(before[k], after[k], p)
    elif before != after:
        out.append(f"~ {path or '(value)'}: {json.dumps(before, ensure_ascii=False)[:200]} -> {json.dumps(after, ensure_ascii=False)[:200]}")
    return out
