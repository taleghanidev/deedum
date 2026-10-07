# Docket

**A git-friendly record of every API call your machine makes: the payload, the response, which
project it came from, which credential it used, and why.**

You change things through APIs all day: ad platforms, CRMs, DNS, payments, cloud consoles. More and
more of it is done by scripts an AI agent wrote a minute ago, or a one-off `curl`. None of it is
logged anywhere you can read back. Docket records each request and response into a folder that
lives in git, so later you (or an agent) can answer "what did we change, when, from where, with
which key, and why" from the record instead of from memory.

```
docket setup --write                                   # once: one trail for every folder on this machine
docket why "raise the brand campaign bids to $14"                     # the reason for this session's calls
docket run --why "check the budget" -- python x.py     # or per command
docket run --dry -- node migrate-dns.mjs               # record and BLOCK every write
docket log --since 7d --writes --here                  # what this project changed
docket projects                                        # which projects called which services
docket secrets                                         # which credential was used where
docket show 20260908-170312-a1b2c3                     # full payload + response of one call
docket report --since 30d                              # markdown summary, for people and agents
```

## What it captures

No code changes. Hooks travel through the environment into every process a shell starts:

- **Python**: `requests`, `httpx`, `urllib`, `urllib3` (botocore / the AWS SDK) and **gRPC**
  (Google Ads, Google Cloud clients)
- **Node**: `fetch` and `http`/`https` (axios, got, the Stripe and AWS SDKs, Next.js server code)
- **curl**: a shim on PATH records the call and passes everything through unchanged
- **GraphQL**: classified by operation: a `mutation` is a write, a `query` is a read
- **Anything else** (a compiled binary such as `aws`, `gh`, `terraform`): `docket run` still
  records the command, its reason and its exit code, so the change is on the record

Reads are marked `read`; a POST that is really a query (`/Query`, `/Search`, `/Report` ...) is a
read. Reads to dev servers, package registries and font CDNs are not recorded (writes to them are);
`docket noise` shows what else is filling the trail and `docket ignore HOST` hides it.

## One trail for every folder, split by project

`docket setup` creates `~/.docket` (its own git repo) and writes a small block to `~/.zshenv`
(`--shell bash` for bash). From then on every folder on the machine records into it. Each call
carries where it came from:

```json
{"project": "acme-crm", "folder": "apps/crm", "branch": "main", "service": "Stripe", ...}
```

The project is the nearest git repo (or a folder holding a `.docket-project` file whose text is
the name; `DOCKET_PROJECT=name` overrides). Outside a repo it is the folder's name. Filter any
listing with `--project NAME` or `--here`. A repo that wants its own separate trail can still run
`docket init` inside it, and a folder can point at another trail with `docket link`.

By default the block switches capture on only in AI-agent shells (Claude Code, Codex, Cursor) or
when `DOCKET_ON=1`; `--when always` turns it on in every shell. Use `~/.zshenv`, not `~/.zshrc`:
agent harnesses run commands in a non-interactive shell that never reads `.zshrc`.

## Secrets: named, never stored

A value is never written. Every secret-named variable in the environment and in `.env*` files up
the folder tree is known by name, so wherever its value would appear the record says which
credential it was instead:

```
"Authorization": "Bearer <secret:STRIPE_SECRET_KEY#a1b2>"
```

The 4-character fingerprint changes when the key is rotated, so `docket secrets` shows which
credential each project and service used, when it was last used, and whether more than one version
of it has been seen. On top of that, known key formats (Stripe, OpenAI, Anthropic, Google, AWS,
GitHub, Slack, JWTs, private keys ...), secret-named headers and body fields, and `name=value`
pairs in URLs and argv are redacted. `docket scan` searches the trail for anything
credential-shaped that slipped through; `docket scan --fix` scrubs the files (rotate the key too:
git history keeps the old line).

## The reason (`why`)

A record without a reason is half a record. Ways to give one, strongest first:

- `docket run --why "..." -- cmd` for one command
- `docket why "..."`: sticks to this terminal or agent session for 12 hours, or until changed
- `docket hook claude` prints Claude Code hooks: each request you type becomes the reason on the
  calls it causes, and the trail is committed when a turn ends
- `docket guard api.stripe.com "*.twilio.com"`: writes to those hosts are refused (the call never
  leaves the machine) unless a reason is set

## Safety

- `docket run --dry -- cmd`: every write is recorded as `BLOCKED` and not sent (Python, Node and
  curl); reads go through, so a script can be rehearsed against the real API
- A hook that fails reports once on stderr and never breaks the program it is in

## What it writes

```
~/.docket/
  CHANGES.md               one line per write: time, [project], service, method, URL, status, why, id
  calls/2026-09-08.jsonl   every call, one JSON per line
  runs/2026-09-08.jsonl    one line per process / docket run: command, project, why, counts, exit code
  config.json              limits, read/write rules, redaction, ignore lists, guarded hosts, services
  README.md                tells an AI agent how to read the folder
```

Plain text, sorted by time, meant to be committed: `docket commit` (or the Claude Code Stop hook,
or `"auto_commit": true`). `docket compact --older 14d` gzips old days (every command reads `.gz`
too); `--strip-read-bodies` drops the response bodies of reads in them. `docket migrate` adds the
project / folder / service fields to records made before 0.2.

## All commands

| Command | |
|---|---|
| `setup` | one machine-wide trail + the shell block (`--write` installs it) |
| `init`, `link` | a trail for one repo; point a folder at another trail |
| `run [--why] [--dry] [--commit] -- cmd` | run a command with capture on |
| `why ["text"] [--clear]` | set or show this session's reason |
| `hook claude` | Claude Code hooks for reasons and commits |
| `log` | timeline; `--writes --reads --project --here --service --host --method --status 4xx --why --secret --run --grep --json --follow` |
| `show ID` | one full record |
| `report` | markdown summary by project and reason (`--json`) |
| `projects`, `secrets`, `runs` | who called what, with which key, from which command |
| `noise`, `ignore HOST`, `guard HOST` | tune what is recorded and what needs a reason |
| `scan [--fix]`, `compact`, `migrate` | housekeeping |
| `status`, `env`, `commit [-q]` | |

## Install

```
pipx install docket        # or: pip install -e . from a checkout
docket setup --write
```

Zero dependencies. Python 3.9+ (Node 18+ for the Node hook). macOS and Linux.

## How the hooks get in

`docket run` / the setup block prepend a `sitecustomize.py` to `PYTHONPATH`, a `curl` shim to
`PATH` and `--require node_hook.cjs` to `NODE_OPTIONS`. Because scripts often set their own
`PYTHONPATH`, docket also drops a tiny, environment-gated `docket_hook.pth` into the site-packages of
the Python it runs (it prints when it does). That file does nothing unless `DOCKET_RUN` is set.

## For AI agents

If you are asked what was changed in an external system, read `CHANGES.md` in the trail
(`docket status` says where it is), then `docket show <id>` for payloads. Set a reason before you
change anything: `docket why "..."` or `docket run --why "..."`.

## Roadmap

- `docket diff ID` / `docket undo ID` for providers whose state can be read back (DNS, Vercel, ads)
- A local proxy mode for runtimes without a hook (Go, Ruby, compiled binaries)
- An MCP server so agents can query the trail directly
- A hosted, shareable timeline

MIT.
