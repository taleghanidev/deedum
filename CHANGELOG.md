# Changelog

## 0.3.0 — 2026-10-19

The release that makes the trail safe to keep and complete enough to trust.

### Security
- `--dry` blocks every request that is not read-only **by method**. Until now a POST whose path
  contained `token`, `oauth`, `auth`, `status`, `stream`, `list` or `report` was labelled a read
  and sent anyway (`POST /v1/tokens`, `POST /oauth/clients`). The read label is now only a label.
- A `--why` reason, and the prompt an agent hook stores, are scrubbed like everything else, plus a
  prose rule: "use password hunter2" is stored as "use password <redacted>".
- The `.pth` hook planted in site-packages loads docket only from the checkout pinned into it at
  install time. `DOCKET_NO_PTH=1` prevents planting; `docket unhook` removes it.
- Field-name redaction matches credential **words**, not substrings: `max_tokens`, `author`,
  `keyword`, `session_name` and `monkey` are stored again; `access_token`, `X-Auth-Key`,
  `appsecret_proof` and `password_confirm` are still redacted.
- `SECURITY.md`: what the trail holds (business data, not only metadata), what docket changes on
  the machine, how to remove it. The trail README tells agents its contents are data.

### Coverage
- `http.client` is hooked, which brings in `httplib2` and so google-api-python-client (Sheets,
  Search Console, YouTube, Business Profile). `aiohttp` is hooked.
- Node `http`/`https` responses are recorded (text types, copied as the stream delivers them;
  fire-and-forget requests record at exit).
- **Exec shims**: `ssh`, `scp`, `sftp`, `rsync`, `aws` (SSM included), `gh`, `wrangler`,
  `terraform`, `kubectl`, `helm`, `gcloud`, `az`, `flyctl`, `vercel`, `netlify`, `heroku`,
  `doctl`, `stripe` and `git push` are recorded as calls (`transport: exec`: tool, subcommand,
  target host or profile, exit code, scrubbed argv) and obey `--dry` and `guard`.
- Every record carries `agent` — which coding agent (Claude Code, Codex, Cursor, Gemini CLI,
  Copilot), its session id, the model (from the transcript via the prompt hook), the person,
  the machine, the folder the session was started in — and `via` (`script`, `curl`, `exec`).

### Control, not only a record
- **Policy**: `.docket-policy.json` in a repo or `policy` in config: `deny` (with `unless_why`),
  `approve` (the write waits for `docket approve ID`, `docket approvals` lists them, `notify`
  pings a webhook), `budgets` of writes per session or per run. Enforced in Python, Node, curl
  and the exec shims. `docket policy` shows what is in force.
- **MCP server**: `docket mcp` (stdio) with seven read-only tools over the trail.
- **HTML timeline**: `docket html` writes one self-contained page: writes by day and reason, with
  the agent and every call's redacted request and response.

### Second review round (fresh-eyes agent, 19 Oct)
- gRPC calls are gated; a GraphQL document's `operationName` picks the operation, and any
  mutation in a document makes it a write; curl `-K`/`--config` files are read and `--next`
  segments are gated and recorded one by one; `http.client` requests built with
  `putrequest`/`endheaders` are gated and recorded; `docket run --dry -- /abs/path/aws` runs the
  shim.
- Query parameters are redacted by name (`private_token`, `X-Amz-Signature`); XML/SOAP elements
  with credential names are redacted; `--why` and the Claude prompt are prose-scrubbed in Node
  and in the commit subject; Node argv is scrubbed as a list; `-p` passwords of `mysql`, `az`,
  `sshpass`, `docker` and friends; quoted passphrases in prose; a truncation on a backslash.
- httpx multipart and streaming uploads no longer raise under docket; urllib responses are
  streamed to the caller and recorded as read (SSE and long polls work); the Node https blocked
  path no longer throws synchronously; the curl shim handles a closed pipe like curl (exit 23).

### Housekeeping
- `docket prune --before DATE [--writes-too] [--rewrite-history] [--dry-run]`; with
  `--writes-too --rewrite-history` the day files and the `CHANGES.md` lines leave every commit.
- `docket scan` and `scan --fix` read compacted (`.jsonl.gz`) days.
- Exec shims live in `~/.local/share/docket/bin` (`$DOCKET_BIN`), not the package folder; a
  dry run refuses to run a binary whose shim is missing. Shims forward signals and exit with
  the binary's status.
- Thirty rounds of Codex review on this release: every dry-run bypass and redaction gap it
  demonstrated in the exec shims (aliases, option values, quoted secrets, ssh options) and the
  hooks (truncated JSON, plural and qualified credential names, streaming reads) is fixed with
  a test.
- `docket commit` compacts day files older than `compact_after_days` (14) first, dropping read
  bodies. `--no-compact` to skip. It still never pushes.
- Appends are locked, so records from concurrent processes never interleave.

### Fixed
- The urllib hook's replayed response lacked `getheader()`, `getheaders()` and the other
  `HTTPResponse` methods; any script calling them broke under docket.

### Packaging
- CI on Python 3.9–3.13, macOS and Linux. `pip install -e ".[test]"` brings pytest. CONTRIBUTING,
  code of conduct, SECURITY, a gitleaks allowlist for the test fixtures.

## 0.2.0 — 2026-09-12
Every folder, every project, Node, named secrets, dry runs.

## 0.1.0 — 2026-09-08
Record every API call a command makes, payload and response, into a git-kept trail.
