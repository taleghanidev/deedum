# Docket

**A git-friendly record of every API call a command makes: the payload, the response, and why.**

You change things through APIs all day — ad platforms, CRMs, DNS, cloud consoles — often through
scripts an AI wrote for you, sometimes with a one-off `curl`. None of it is logged anywhere. Docket
records each request and response into a folder that lives in git, so later you (or an AI) can read
back exactly what was executed, in order, with the reason.

```
docket init                                                   # once, in the repo that keeps the history
docket run --why "raise the brand campaign bids to $14" -- python raise_bids.py --apply
docket run --why "check the budget"      -- curl -s https://api.example.com/v1/budget
docket log --since 7d --writes                                # what was changed
docket show 20260908-170312-a1b2c3                            # full payload + response of one call
docket report --since 30d                                     # markdown summary, for people and AI
```

No code changes. `docket run` starts the command with hooks that capture:

- Python: `requests`, `httpx`, `urllib` and **gRPC** (Google Ads, Google Cloud clients)
- `curl` — a shim on PATH records the call and passes everything through unchanged
- GraphQL — classified by operation: a `mutation` is a write, a `query` is a read
- anything they spawn (the hooks travel through the environment)

Reads are logged too and marked `read`; a POST that is really a query (`/Query`, `/Search`, `/Report`…)
is classified as a read. Secrets in headers and well-known body fields are redacted before writing.

## Always-on mode (for AI agent sessions)

Instead of remembering `docket run`, turn capture on for a whole shell:

```
eval "$(docket env)"        # in ~/.zshrc, e.g. only when $CLAUDECODE is set
```

Every Python and curl started from that shell is then recorded, no wrapper needed. Add a reason
to a specific command with `DOCKET_WHY="raise bids" python x.py`, or keep using `docket run --why`
for changes. `docket commit` (or a session-end hook) commits what was gathered.

## What it writes

```
.docket/
  CHANGES.md            one line per write — start here
  calls/2026-09-08.jsonl  every call, one JSON per line (id, ts, run, why, kind, method, host, path, request, status, response, ms)
  runs/2026-09-08.jsonl   one line per docket run: command, why, counts, exit code
  config.json           truncation limits, read/write rules, redaction lists
  README.md             tells an AI agent how to read the folder
```

Plain text, sorted by time, meant to be committed. `docket run --commit` commits after each run;
`"auto_commit": true` in `config.json` does it always. `git log -- .docket` is then the change history
of everything you did to the outside world.

## Install

```
pip install docket          # or: pipx install docket
```

or from a checkout: `pip install -e .`

Zero dependencies. Python 3.9+. macOS and Linux.

## How the hooks get in

`docket run` prepends a `sitecustomize.py` to `PYTHONPATH` and a `curl` shim to `PATH`. Because
scripts often set their own `PYTHONPATH`, it also drops a tiny, environment-gated
`docket_hook.pth` into the site-packages of the Python it is about to run (it prints when it does).
That file does nothing unless `DOCKET_RUN` is set, so normal runs of that Python are unaffected.

## Using it from another directory

Keep one trail for a whole business, and point other working directories at it:

```
cd ~/tools/ads && docket link ~/company-repo      # writes a .docket pointer file here
```

or `export DOCKET_DIR=~/company-repo/.docket`.

## For AI agents

If you are asked what was changed in an external system, read `.docket/CHANGES.md`, then
`docket show <id>` for the payloads. Always run changes through `docket run --why "..."` so the
next agent can do the same.

## Roadmap

- Node hook (`NODE_OPTIONS=--require`)
- Before/after values for known APIs, and `docket undo <id>`
- A hosted, shareable timeline — GitHub for API actions

MIT.
