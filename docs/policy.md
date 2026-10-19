# Policy: what a call from here may do

Deedum records everything by default and refuses nothing. A policy changes that: a write can be
refused, held for a person, or counted against a budget, before it leaves the machine. The same
policy is enforced in the Python hooks, the Node hook, the curl shim and the binary shims.

## Where a policy lives

1. `policy` in the trail's `config.json` (`deedum status` says where the trail is). These rules
   are evaluated first and cannot be loosened by a repository.
2. `.deedum-policy.json` in a repository, found by walking up from the working directory. In git,
   beside the code, so a team shares it.

`deedum policy` shows what is in force from the current folder; `deedum policy --example` prints
a template; `deedum policy --json` the merged result.

## The file

```json
{
  "rules": [
    {"host": "api.stripe.com", "deny": true, "unless_why": true},
    {"host": "*.googleapis.com", "methods": ["DELETE"], "deny": true, "note": "no deletes from scripts"},
    {"host": "api.cloudflare.com", "approve": true, "timeout": 600},
    {"host": "*", "project": "scratch", "deny": true},
    {"host": "api.internal.example.com", "allow": true}
  ],
  "budgets": [
    {"host": "api.stripe.com", "writes": 20, "per": "session"},
    {"host": "*", "writes": 500, "per": "run"}
  ],
  "notify": "https://api.telegram.org/bot<secret:TG_BOT_TOKEN>/sendMessage?chat_id=123"
}
```

### Rules

The first rule that matches a write decides. A rule matches when every field it names matches:

| Field | Meaning |
|---|---|
| `host` | a glob over the host name (`*.googleapis.com`, `*`) |
| `methods` | a list of methods; absent means any. Binaries use their name (`SSH`, `AWS`, `GIT`) |
| `project` | a glob over the project name (the repository folder, or `.deedum-project`) |
| `kind` | `write` (default), `read` or `any`; rules apply to writes unless told otherwise |
| `deny` | refuse the call |
| `unless_why` | with `deny`: let it through once a reason is set (`deedum why`, `--why`) |
| `approve` | hold the call until a person runs `deedum approve ID` |
| `timeout` | with `approve`: seconds to wait before refusing (default 600) |
| `allow` | let it through, stop looking at later rules |
| `note` | text added to the refusal message |

A refused call is recorded with `status: BLOCKED` and the reason in `error`. The program sees a
`PermissionError` (Python), a rejected promise (Node) or exit code 7 (curl, binaries).

### Approvals

With `approve`, the calling process writes a request under the trail's `.state/approvals/`,
prints

```
[deedum] PUT api.cloudflare.com/zones/abc/dns_records/1 from infra (change the MX) waits for approval: deedum approve 3f9a1c2d   (deny: deedum deny 3f9a1c2d; 600s)
```

and waits. In another terminal, or on another machine with the same trail, a person runs
`deedum approvals` to see what is waiting and `deedum approve ID` or `deedum deny ID`. The
`notify` webhook, when set, receives that same line as `{"text": ...}` the moment the request is
made, so a Telegram or Slack message says what an agent is about to do.

### Budgets

A budget counts the writes already recorded to matching hosts in the same session (a Claude Code
session, else this terminal) or the same `deedum run`, and refuses the one that would go past
`writes`. The count is read from the trail's day files, so it survives restarts.

## Snapshots

Separate from policy but related: `deedum snapshot HOST` lists hosts whose objects are read and
kept (`before`) just ahead of each PUT, PATCH or DELETE made from Python or curl, using the
request's own credentials, and only once the write is cleared to go (a dry run or a refused
write touches nothing, not even with a GET). `deedum diff ID` and `deedum undo ID` need it. It
costs one GET per write to those hosts. Writes made from Node record their `origin` but take no
snapshot yet.

## What a policy does not do

It does not inspect bodies (a rule cannot say "no refunds over $100"), it does not apply to
binaries without a shim, and it does not stop a program that opens a raw socket. It is a seat
belt for agents using ordinary clients, which is what they use.
