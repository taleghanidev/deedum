"""docket command line."""
import os, sys, json, argparse, subprocess, pathlib, uuid, textwrap, collections, shlex, fnmatch, gzip, time, re
from . import store, __version__

PKG_DIR = str(pathlib.Path(__file__).resolve().parent.parent)
SHIM_DIR = str(pathlib.Path(__file__).resolve().parent / "shim")
NODE_HOOK = str(pathlib.Path(SHIM_DIR) / "node_hook.cjs")
# The per-binary shims (ssh, aws, gh ...) are symlinks made at run time; they live in a folder
# of the person's own ($DOCKET_BIN, else ~/.local/share/docket/bin), because a package folder
# under a system Python may be read-only.
BIN_DIR = str(pathlib.Path(os.path.expanduser(os.environ.get("DOCKET_BIN") or "~/.local/share/docket/bin")))
MARK_START, MARK_END = "# >>> docket >>>", "# <<< docket <<<"


def _dir(args, required=True):
    d = store.find_dir(getattr(args, "dir", None))
    if d is None and required:
        sys.exit("docket: no trail found. Run `docket setup` once (one trail for every folder on this "
                 "machine), or `docket init` inside a repo for a repo-only trail, or set DOCKET_DIR.")
    return d


def _out_json(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


# ---- setup / init / link ---------------------------------------------------------------------
def cmd_init(args):
    target = pathlib.Path(args.path or ".").resolve() / store.DIRNAME
    store.init_dir(target)
    root = store.git_root(target)
    print(f"initialised {target}")
    print("inside git repo:", root or "NO: `git init` here if you want history kept")
    print('next: docket run --why "what you are about to change" -- <command>')


def _env_lines(d):
    q = shlex.quote
    return [
        f"export DOCKET_DIR={q(str(d))}",
        "export DOCKET_RUN=always",
        f"export DOCKET_PKG={q(PKG_DIR)}",
        f'export PYTHONPATH={q(SHIM_DIR)}"${{PYTHONPATH:+:$PYTHONPATH}}"',
        f'export PATH={q(BIN_DIR)}:{q(SHIM_DIR)}:"$PATH"',
        # Node: fetch, http and https in every node process (Next.js, scripts, SDKs).
        f'export NODE_OPTIONS="--require {NODE_HOOK}${{NODE_OPTIONS:+ $NODE_OPTIONS}}"',
        # Survives a harness that resets PATH from a snapshot: one function per shim that exists
        # (made here if it can be; a function for a missing file would shadow the real binary).
        "; ".join([f'curl() {{ {q(str(pathlib.Path(SHIM_DIR) / "curl"))} "$@"; }}'] +
                  [f'{n}() {{ {q(str(pathlib.Path(BIN_DIR) / n))} "$@"; }}' for n in ensure_exec_shims()]),
    ]


def cmd_setup(args):
    """One trail for the whole machine: create it (as its own git repo), then print, or with
    --write install, the shell block that turns capture on in every folder."""
    d = pathlib.Path(os.path.expanduser(args.path)).resolve() if args.path else (_dir(args, required=False) or store.global_dir())
    if not (d / "calls").is_dir():
        store.init_dir(d)
        print(f"created trail {d}")
    if not store.git_root(d):
        subprocess.run(["git", "init", "-q", str(d)], check=False)
        print(store.git_commit(d, "docket: start the trail"))
    ensure_exec_shims()
    guard = {"agents": '[ -n "$CLAUDECODE$CODEX_SANDBOX$CURSOR_AGENT$DOCKET_ON" ]', "always": "true"}[args.when]
    lines = _env_lines(d)
    # The trail location is exported for every shell, so `docket log` works in any terminal;
    # capture itself only switches on inside the guard.
    block = "\n".join([MARK_START,
                       "# docket: record every API call made from this machine's shells (any folder).",
                       "# ~/.zshenv, not ~/.zshrc: agent shells are non-interactive and never read .zshrc.",
                       lines[0],
                       f'if {guard} && [ -d {shlex.quote(SHIM_DIR)} ]; then',
                       # Child shells inherit the exports: setting them again would stack the
                       # hooks, and inside `docket run` would overwrite that run's id.
                       '  if [ -z "$DOCKET_RUN" ]; then',
                       *("    " + ln for ln in lines[1:-1]), "  fi",
                       "  " + lines[-1],  # functions are not inherited, so always define curl()
                       "fi", MARK_END])
    profile = pathlib.Path(os.path.expanduser(args.profile or {"zsh": "~/.zshenv", "bash": "~/.bashrc"}[args.shell]))
    if not args.write:
        print(f"\nAdd this to {profile} (or rerun with --write to do it):\n")
        print(block)
        if args.shell == "zsh":
            print("\n~/.zshenv, not ~/.zshrc: agent shells are non-interactive and never read .zshrc.")
        return
    text = profile.read_text() if profile.exists() else ""
    if MARK_START in text and MARK_END in text:
        a, b = text.index(MARK_START), text.index(MARK_END) + len(MARK_END)
        text = text[:a] + block + text[b:]
    else:
        text = text + ("" if text.endswith("\n") or not text else "\n") + "\n" + block + "\n"
    profile.write_text(text)
    print(f"wrote the docket block to {profile}")
    stray = [ln for ln in text.split(MARK_END)[-1].splitlines() + text.split(MARK_START)[0].splitlines()
             if "DOCKET_" in ln and not ln.strip().startswith("#")]
    if stray:
        print(f"note: {profile} also sets DOCKET_ outside the block; remove those lines:")
        for ln in stray:
            print("   ", ln)


def cmd_link(args):
    """Write a `.docket` pointer file in cwd so runs from here log into another repo's trail."""
    target = pathlib.Path(os.path.expanduser(args.target)).resolve()
    if target.name != store.DIRNAME:
        target = target / store.DIRNAME
    if not target.is_dir():
        sys.exit(f"{target} is not an initialised trail (run `docket init` there first)")
    pathlib.Path(store.DIRNAME).write_text(str(target) + "\n")
    print(f".docket -> {target}")


# ---- run -------------------------------------------------------------------------------------
def cmd_run(args):
    d = _dir(args)
    if not args.command:
        sys.exit("docket run: give a command after `--`")
    run_id = uuid.uuid4().hex[:8]
    env = dict(os.environ)
    env["DOCKET_DIR"] = str(d)
    env["DOCKET_RUN"] = run_id
    env["DOCKET_PKG"] = PKG_DIR
    if args.why:
        env["DOCKET_WHY"] = args.why
    if args.dry:
        env["DOCKET_DRY"] = "1"
    env["PYTHONPATH"] = SHIM_DIR + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PATH"] = BIN_DIR + os.pathsep + SHIM_DIR + os.pathsep + env.get("PATH", "")
    if NODE_HOOK not in env.get("NODE_OPTIONS", ""):
        env["NODE_OPTIONS"] = f'--require "{NODE_HOOK}"' + (" " + env["NODE_OPTIONS"] if env.get("NODE_OPTIONS") else "")
    args.command = _merge_pythonpath(args.command)
    _install_pth(args.command)
    have = ensure_exec_shims()
    idx, first = next(((i, t) for i, t in enumerate(args.command) if t != "env" and "=" not in t), (None, ""))
    if os.sep in first and os.path.basename(first) in EXEC_SHIMS and os.path.basename(first) in have:
        # `/usr/local/bin/aws ...` would skip the shim on PATH: run the shim, which runs that binary
        env["DOCKET_REAL_BIN"] = first
        args.command[idx] = os.path.basename(first)
    if args.dry and os.path.basename(first) in EXEC_SHIMS and os.path.basename(first) not in have:
        # Fail closed: without its shim the real binary would run, and a dry run promised it would not.
        sys.exit(f"docket run --dry: the {os.path.basename(first)} shim could not be made in {BIN_DIR} "
                 f"(set DOCKET_BIN to a writable folder); refusing to run the real binary")
    started = store.now_iso()
    try:
        code = subprocess.call(args.command, env=env)
    except KeyboardInterrupt:
        code = 130
    except FileNotFoundError:
        sys.exit(f"docket run: command not found: {args.command[0]}")
    calls = [r for r in store.iter_records(d, "calls", since=started[:10]) if r.get("run") == run_id]
    writes = [r for r in calls if r["kind"] == "write"]
    blocked = [r for r in calls if r.get("status") == "BLOCKED"]
    hosts = sorted({r["host"] for r in calls})
    from .recorder import scrub_values, scrub_argv_list, scrub_prose, Recorder
    why = scrub_prose(args.why or store.get_session_why(d) or "")
    proj = store.project_of()
    argv = Recorder(d).scrub_argv(scrub_argv_list(args.command))[:2000]
    # Every `docket run` leaves a command record, even when nothing was captured: a compiled
    # binary (the aws CLI, gh, terraform) makes calls no hook can see, and the command, its
    # reason and its exit code are still the record of what was done.
    rec = {"type": "command", "run": run_id, "argv": argv, "cwd": str(pathlib.Path.cwd()), "why": why,
           "project": proj["name"], "folder": proj["folder"], "branch": proj["branch"], "dry": bool(args.dry),
           "started": started, "ended": store.now_iso(), "calls": len(calls), "writes": len(writes),
           "exit": code}
    (d / "runs").mkdir(exist_ok=True)
    store.append_line(d / "runs" / f"{rec['ended'][:10]}.jsonl", scrub_values(json.dumps(rec, ensure_ascii=False)))
    if not calls:
        pj = f"[{proj['name']}] " if proj["name"] else ""
        store.append_line(d / "CHANGES.md", scrub_values(f"- {started[:16].replace('T', ' ')} · {pj}command · `{argv[:300]}` · exit {code}"
                                                         f"{' · why: ' + why if why else ''} · no API calls captured (run {run_id})"))
    print(f"\n[docket] run {run_id}{' (dry)' if args.dry else ''}: {len(calls)} calls ({len(writes)} writes"
          f"{f', {len(blocked)} blocked' if blocked else ''}) to {', '.join(hosts) or 'nothing captured'}; exit {code}",
          file=sys.stderr)
    for w in writes:
        st = w["status"] if w["status"] is not None else w["error"]
        print(f"[docket]   {w['method']} {w['host']}{w['path']} -> {st}  `{w['id']}`", file=sys.stderr)
    if not calls:
        print("[docket]   no hookable calls (compiled binary?): the command and reason were recorded", file=sys.stderr)
    cfg = store.load_config(d)
    if args.commit or cfg.get("auto_commit"):
        msg = f"docket: {why or argv[:60]}"
        body = f"\n\nrun {run_id}: {len(calls)} calls, {len(writes)} writes, exit {code}\n" + "\n".join(
            f"- {w['method']} {w['host']}{w['path']} -> {w['status'] or w['error']} ({w['id']})" for w in writes)
        print("[docket] git:", store.git_commit(d, msg + body), file=sys.stderr)
    sys.exit(code)


def _merge_pythonpath(cmd):
    """`env PYTHONPATH=. python x.py` would replace our hook path: prepend the shim to it."""
    out = []
    for tok in cmd:
        if tok.startswith("PYTHONPATH="):
            tok = "PYTHONPATH=" + SHIM_DIR + os.pathsep + tok[len("PYTHONPATH="):]
        out.append(tok)
    return out


def hook_source():
    """docket_hook.py with this checkout's package path pinned in: the planted hook loads docket
    from here and nowhere else, whatever DOCKET_PKG says."""
    src = (pathlib.Path(SHIM_DIR) / "docket_hook.py").read_text()
    return src.replace("_EXPECTED = None", f"_EXPECTED = {PKG_DIR!r}", 1)


def plant_hook(site=None, quiet=True):
    """Write the env-gated hook into a site-packages (this interpreter's by default). Returns
    True when something was written. Set DOCKET_NO_PTH=1 to never plant it."""
    if os.environ.get("DOCKET_NO_PTH"):
        return False
    if site is None:
        import sysconfig
        site = sysconfig.get_paths()["purelib"]
    site = pathlib.Path(site)
    if not site.is_dir():
        return False
    hook, pth, src = site / "docket_hook.py", site / "docket_hook.pth", hook_source()
    wrote = False
    try:
        if not hook.exists() or hook.read_text() != src:
            hook.write_text(src); wrote = True
        if not pth.exists():
            pth.write_text("import docket_hook\n"); wrote = True
    except OSError:
        return False
    if wrote and not quiet:
        print(f"[docket] hook installed in {site} (DOCKET_NO_PTH=1 prevents this; docket unhook removes it)", file=sys.stderr)
    return wrote


def _install_pth(cmd):
    """If the command runs a Python interpreter, drop an env-gated .pth hook into its site-packages
    so the hooks load even when the script rewrites PYTHONPATH or spawns clean subprocesses."""
    import shutil
    exe = None
    for tok in cmd:
        if tok == "env" or "=" in tok:
            continue
        exe = tok; break
    if not exe or not os.path.basename(exe).startswith("python"):
        return
    path = shutil.which(exe) if os.sep not in exe else exe
    if not path:
        return
    try:
        site = subprocess.run([path, "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"],
                              capture_output=True, text=True, timeout=20).stdout.strip()
        if site:
            plant_hook(site, quiet=False)
    except (PermissionError, OSError, subprocess.TimeoutExpired):
        pass


def cmd_unhook(args):
    """Remove every docket_hook.pth this machine's Pythons have: this interpreter's, plus any
    path given. `docket unhook --find` lists the ones under ~ without removing them."""
    import sysconfig
    sites = [pathlib.Path(sysconfig.get_paths()["purelib"])] + [pathlib.Path(p) for p in args.sites]
    if args.find:
        home = pathlib.Path.home()
        sites += [p.parent for p in home.glob("**/site-packages/docket_hook.pth")]
    n = 0
    for site in sites:
        for name in ("docket_hook.pth", "docket_hook.py"):
            f = site / name
            if f.exists():
                if args.find and not args.remove:
                    print(f)
                else:
                    f.unlink(); n += 1; print(f"removed {f}")
    if not args.find or args.remove:
        print(f"{n} file(s) removed")


def cmd_env(args):
    """Print the shell exports that turn on capture for every Python, Node and curl started from
    this shell. Prefer `docket setup --write`, which installs them for every folder."""
    for ln in _env_lines(_dir(args)):
        print(ln)


# ---- the reason ------------------------------------------------------------------------------
def cmd_why(args):
    d = _dir(args)
    if args.from_claude_hook:
        # Claude Code UserPromptSubmit hook: the user's request becomes the reason for every call
        # until the next request. Prints nothing (stdout would be added to the conversation).
        try:
            data = json.load(sys.stdin)
        except Exception:
            return
        prompt = " ".join(str(data.get("prompt") or "").split())
        if not prompt or prompt.startswith("/"):
            return
        from .recorder import scrub_prose
        text = scrub_prose(prompt)
        why = "asked: " + (text[:200] + ("…" if len(text) > 200 else ""))
        store.set_session_why(d, why, key=data.get("session_id"), agent=_agent_from_hook(data))
        return
    if args.clear:
        store.set_session_why(d, None)
        print("reason cleared for this session")
        return
    if args.text:
        from .recorder import scrub_prose
        why = scrub_prose(" ".join(args.text))
        store.set_session_why(d, why)
        print(f"reason for this session (next {store.WHY_TTL_HOURS}h, until changed): {why}")
        return
    cur = os.environ.get("DOCKET_WHY") or store.get_session_why(d)
    print(cur or "(no reason set: docket why \"what you are doing\")")


def _agent_from_hook(data):
    """What a Claude Code hook payload says about the agent: its session, the folder the
    session was started in, and the model (from the transcript, which names it on every
    assistant message; the payload itself does not)."""
    out = {"name": "claude-code", "session": data.get("session_id"), "started_in": data.get("cwd"), "model": data.get("model")}
    if not out["model"] and data.get("transcript_path"):
        try:
            with open(data["transcript_path"], "rb") as f:
                f.seek(max(0, os.path.getsize(data["transcript_path"]) - 200_000))
                m = re.findall(rb'"model"\s*:\s*"([^"]{3,80})"', f.read())
                if m:
                    out["model"] = m[-1].decode()
        except Exception:
            pass
    return {k: v for k, v in out.items() if v}


# Binaries no hook can see from inside (ssh, the AWS CLI with SSM, gh, terraform ...): a shim
# of the same name on PATH records the command as a call and runs the real one. `git` is
# recorded only for push and remote-changing subcommands (shim/_exec).
EXEC_SHIMS = ["ssh", "scp", "sftp", "rsync", "aws", "gh", "wrangler", "terraform", "kubectl", "helm",
              "gcloud", "az", "flyctl", "fly", "vercel", "netlify", "heroku", "doctl", "stripe", "git"]


def ensure_exec_shims():
    """Make the per-binary symlinks to shim/_exec in BIN_DIR. Returns the names that exist."""
    src = pathlib.Path(SHIM_DIR) / "_exec"
    have = []
    try:
        pathlib.Path(BIN_DIR).mkdir(parents=True, exist_ok=True)
    except OSError:
        return have
    for name in EXEC_SHIMS:
        link = pathlib.Path(BIN_DIR) / name
        try:
            if link.is_symlink() and os.readlink(link) != str(src):
                link.unlink()   # an older docket's shim: point it at this one
            if not (link.is_symlink() or link.exists()):
                link.symlink_to(src)
            if link.exists():
                have.append(name)
        except OSError:
            pass
    return have


def cmd_hook(args):
    exe = shlex.quote(sys.argv[0] if os.path.isabs(sys.argv[0]) else "docket")
    cfg = {"hooks": {
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": f"{exe} why --from-claude-hook"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": f"{exe} commit -q"}]}],
    }}
    print("Add to ~/.claude/settings.json (merge with any hooks already there):\n")
    print(json.dumps(cfg, indent=2))
    print("\nUserPromptSubmit: each request you type becomes the reason on the calls it causes.")
    print("Stop: the trail is committed when Claude finishes a turn.")


# ---- reading ---------------------------------------------------------------------------------
def _status_match(st, want):
    if not want:
        return True
    s = str(st)
    if re.fullmatch(r"\dxx", want):
        return s[:1] == want[0] and s.isdigit()
    return s.lower() == want.lower()


def _filter(rows, args, since=None):
    here = store.project_of()["name"] if getattr(args, "here", False) else None
    for r in rows:
        if since and r.get("ts", "") < since:
            continue
        if getattr(args, "writes", False) and r.get("kind") != "write":
            continue
        if getattr(args, "reads", False) and r.get("kind") != "read":
            continue
        if here and r.get("project") != here:
            continue
        if getattr(args, "project", None) and not fnmatch.fnmatch(r.get("project") or "", args.project):
            continue
        if getattr(args, "host", None) and args.host not in (r.get("host") or ""):
            continue
        if getattr(args, "service", None) and args.service.lower() not in (r.get("service") or r.get("host") or "").lower():
            continue
        if getattr(args, "method", None) and (r.get("method") or "").upper() != args.method.upper():
            continue
        if getattr(args, "status", None) and not _status_match(r.get("status"), args.status):
            continue
        if getattr(args, "why", None) and args.why.lower() not in (r.get("why") or "").lower():
            continue
        if getattr(args, "run", None) and r.get("run") != args.run:
            continue
        if getattr(args, "secret", None) and args.secret not in (r.get("secrets") or []):
            continue
        if getattr(args, "grep", None) and args.grep.lower() not in json.dumps(r).lower():
            continue
        yield r


def _where(r):
    p = r.get("project") or "-"
    f = r.get("folder")
    return f"{p}/{f}" if f else p


def _fmt_row(r):
    st = r["status"] if r.get("status") is not None else (r.get("error") or "")[:24]
    why = f"  why: {r['why'][:80]}" if r.get("why") else ""
    svc = r.get("service") or r.get("host")
    return (f"{r['ts'][:16].replace('T', ' ')}  {_where(r)[:28]:28}  {r['kind']:5} {r['method']:6} "
            f"{svc[:18]:18} {r['host']}{r['path']}  {st}{why}  `{r['id']}`")


def cmd_log(args):
    d = _dir(args)
    since = store.parse_since(args.since)
    rows = list(_filter(store.iter_records(d, "calls", since=since), args, since))
    if args.json:
        _out_json(rows[-args.limit:])
    elif not rows and not args.follow:
        print("no calls match")
    else:
        for r in rows[-args.limit:]:
            print(_fmt_row(r))
        if rows:
            print(f"-- {len(rows)} calls ({sum(1 for r in rows if r['kind'] == 'write')} writes)")
    if args.follow:
        _follow(d, args)


def _follow(d, args):
    """tail -f the trail, through the same filters. Ctrl-C to stop."""
    pos = {}
    for f in (d / "calls").glob("*.jsonl"):
        pos[f] = f.stat().st_size
    try:
        while True:
            for f in sorted((d / "calls").glob("*.jsonl")):
                size = f.stat().st_size
                if size > pos.get(f, 0):
                    with open(f, encoding="utf-8") as fh:
                        fh.seek(pos.get(f, 0))
                        chunk = fh.read()
                    pos[f] = size
                    rows = []
                    for ln in chunk.splitlines():
                        try:
                            rows.append(json.loads(ln))
                        except ValueError:
                            pass
                    for r in _filter(rows, args):
                        print(json.dumps(r, ensure_ascii=False) if args.json else _fmt_row(r), flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def cmd_show(args):
    d = _dir(args)
    day = f"{args.id[:4]}-{args.id[4:6]}-{args.id[6:8]}"
    for r in store.iter_records(d, "calls", since=day, until=day):
        if r["id"] == args.id:
            _out_json(r)
            return
    sys.exit(f"no record {args.id}")


def _top(counter, n=5):
    return ", ".join(f"{k} {v}" for k, v in counter.most_common(n))


def cmd_report(args):
    d = _dir(args)
    since = store.parse_since(args.since)
    rows = list(_filter(store.iter_records(d, "calls", since=since), args, since))
    writes = [r for r in rows if r["kind"] == "write"]
    if args.json:
        _out_json({"since": since, "calls": len(rows), "writes": writes})
        return
    no_why = sum(1 for w in writes if not w.get("why"))
    print(f"# API changes since {since or 'the beginning'}\n")
    print(f"{len(rows)} calls, {len(writes)} writes ({no_why} without a reason), "
          f"{len({r['run'] for r in rows})} runs\n")
    by_proj = collections.defaultdict(collections.Counter)
    for w in writes:
        by_proj[w.get("project") or "(no project)"][w.get("service") or w["host"]] += 1
    if by_proj:
        print("| Project | Writes | Services |\n|---|---|---|")
        for p, c in sorted(by_proj.items(), key=lambda kv: -sum(kv[1].values())):
            print(f"| {p} | {sum(c.values())} | {_top(c, 4)} |")
        print()
    by_day = collections.OrderedDict()
    for w in writes:
        by_day.setdefault(w["ts"][:10], []).append(w)
    for day, ws in by_day.items():
        print(f"## {day}\n")
        groups = collections.OrderedDict()
        for w in ws:
            groups.setdefault((w.get("project") or "(no project)", w.get("why") or "(no reason given)"), []).append(w)
        for (proj, why), group in groups.items():
            print(f"**[{proj}] {why}** ({len(group)} write{'s' if len(group) != 1 else ''})\n")
            for w in group[: args.per_group]:
                body = (w.get("request") or {}).get("body") or {}
                j = body.get("_json") if isinstance(body, dict) else None
                summary = json.dumps(j, ensure_ascii=False) if j is not None else ""
                summary = summary[: args.width] + ("…" if len(summary) > args.width else "")
                st = w["status"] if w.get("status") is not None else w.get("error")
                print(f"- {w['ts'][11:16]} {w.get('service') or w['host']}: {w['method']} {w['host']}{w['path']} → {st} `{w['id']}`")
                if summary:
                    print(f"  `{summary}`")
            if len(group) > args.per_group:
                print(f"- … {len(group) - args.per_group} more (docket log --writes --why {json.dumps(why[:30])})")
            print()


def cmd_projects(args):
    d = _dir(args)
    since = store.parse_since(args.since)
    agg = {}
    for r in store.iter_records(d, "calls", since=since):
        if since and r["ts"] < since:
            continue
        p = r.get("project") or "(no project)"
        a = agg.setdefault(p, {"project": p, "calls": 0, "writes": 0, "last": "", "services": collections.Counter(),
                               "folders": set(), "branches": set()})
        a["calls"] += 1
        a["writes"] += r["kind"] == "write"
        a["last"] = max(a["last"], r["ts"])
        if r["kind"] == "write":
            a["services"][r.get("service") or r["host"]] += 1
        if r.get("folder"):
            a["folders"].add(r["folder"])
        if r.get("branch"):
            a["branches"].add(r["branch"])
    rows = sorted(agg.values(), key=lambda a: a["last"], reverse=True)
    if args.json:
        _out_json([{**a, "services": dict(a["services"]), "folders": sorted(a["folders"]),
                    "branches": sorted(a["branches"])} for a in rows])
        return
    if not rows:
        print("no calls in that window (projects are recorded from docket 0.2 on)")
        return
    print(f"{'project':24} {'writes':>7} {'calls':>7}  {'last':16}  top services written to")
    for a in rows:
        print(f"{a['project'][:24]:24} {a['writes']:>7} {a['calls']:>7}  {a['last'][:16].replace('T', ' ')}  {_top(a['services'], 3)}")


def cmd_secrets(args):
    """Which credential was used where: built from the <secret:NAME#fp> tags, never from values."""
    d = _dir(args)
    since = store.parse_since(args.since)
    tag_re = re.compile(r"<secret:([A-Za-z_][A-Za-z0-9_]*)#([0-9a-f]{4})>")
    agg = {}
    for r in store.iter_records(d, "calls", since=since):
        if since and r["ts"] < since:
            continue
        names = set(r.get("secrets") or [])
        fps = tag_re.findall(json.dumps(r))
        names |= {n for n, _ in fps}
        for n in names:
            a = agg.setdefault(n, {"secret": n, "calls": 0, "writes": 0, "last": "", "services": collections.Counter(),
                                   "projects": collections.Counter(), "fingerprints": set()})
            a["calls"] += 1
            a["writes"] += r["kind"] == "write"
            a["last"] = max(a["last"], r["ts"])
            a["services"][r.get("service") or r["host"]] += 1
            a["projects"][r.get("project") or "-"] += 1
            a["fingerprints"] |= {f for nn, f in fps if nn == n}
    rows = sorted(agg.values(), key=lambda a: a["last"], reverse=True)
    if args.json:
        _out_json([{**a, "services": dict(a["services"]), "projects": dict(a["projects"]),
                    "fingerprints": sorted(a["fingerprints"])} for a in rows])
        return
    if not rows:
        print("no named secrets seen (secrets are named from docket 0.2 on)")
        return
    for a in rows:
        rot = f"  versions seen: {', '.join(sorted(a['fingerprints']))}" if len(a["fingerprints"]) > 1 else ""
        print(f"{a['secret']}\n   {a['calls']} calls ({a['writes']} writes), last {a['last'][:16].replace('T', ' ')}"
              f"\n   services: {_top(a['services'], 4)}\n   projects: {_top(a['projects'], 4)}{rot}")


def cmd_runs(args):
    d = _dir(args)
    since = store.parse_since(args.since)
    rows = [r for r in store.iter_records(d, "runs", since=since)
            if (not since or (r.get("ended") or "") >= since)
            and (not args.project or fnmatch.fnmatch(r.get("project") or "", args.project))]
    if args.json:
        _out_json(rows[-args.limit:])
        return
    for r in rows[-args.limit:]:
        n = r.get("calls", (r.get("reads") or 0) + (r.get("writes") or 0))
        why = f"  why: {r['why'][:70]}" if r.get("why") else ""
        print(f"{(r.get('ended') or '')[:16].replace('T', ' ')}  {_where(r)[:28]:28} {r.get('run', ''):16} "
              f"{n:>4} calls {r.get('writes', 0):>3} writes  exit {r.get('exit')}  {(r.get('argv') or '')[:60]}{why}")


def cmd_noise(args):
    """The hosts filling the trail with reads: candidates for `docket ignore`."""
    d = _dir(args)
    since = store.parse_since(args.since)
    reads, writes = collections.Counter(), collections.Counter()
    for r in store.iter_records(d, "calls", since=since):
        (reads if r["kind"] == "read" else writes)[r["host"]] += 1
    total = sum(reads.values()) + sum(writes.values())
    print(f"{total} calls since {since}; top hosts by reads:\n")
    for h, n in reads.most_common(args.limit):
        print(f"{n:>8} reads {writes[h]:>6} writes  {h}")
    print("\nHide one's reads (writes stay recorded): docket ignore HOST")


def cmd_ignore(args):
    d = _dir(args)
    cfg_path = d / "config.json"
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    key = "ignore_hosts" if args.all else "ignore_read_hosts"
    lst = cfg.get(key) or list(store.DEFAULT_CONFIG[key])
    for h in args.hosts:
        if h not in lst:
            lst.append(h)
    cfg[key] = lst
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"{key}: {', '.join(args.hosts)} added")


def cmd_guard(args):
    """require_why_hosts: a write to these hosts is refused unless a reason is set."""
    d = _dir(args)
    cfg_path = d / "config.json"
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    lst = cfg.get("require_why_hosts") or []
    for h in args.hosts:
        if args.remove:
            lst = [x for x in lst if x != h]
        elif h not in lst:
            lst.append(h)
    cfg["require_why_hosts"] = lst
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
    print("writes need a reason for:", ", ".join(lst) or "(none)")


def cmd_scan(args):
    """Look through the trail for anything that still looks like a credential."""
    from .recorder import _VALUE_RES, _secret_values, scrub_values
    d = _dir(args)
    hits = []
    for sub in ("calls", "runs"):
        for f in sorted(list((d / sub).glob("*.jsonl")) + list((d / sub).glob("*.jsonl.gz"))):   # compacted days too
            opener = gzip.open if f.suffix == ".gz" else open
            with opener(f, "rt", encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            changed = False
            for i, ln in enumerate(lines):
                found = [n for v, n in _secret_values() if v in ln] + [r.pattern[:30] for r in _VALUE_RES if r.search(ln)]
                if found:
                    rid = (re.search(r'"(?:id|run)": "([^"]+)"', ln) or [None, "?"])[1]
                    hits.append((f.name, rid, found))
                    if args.fix:
                        lines[i] = scrub_values(ln)
                        changed = True
            if changed:
                with opener(f, "wt", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
    for name, rid, found in hits[:200]:
        print(f"{name} {rid}: {', '.join(found)}")
    print(f"{len(hits)} record(s) with something credential-shaped" + (" (rewritten)" if args.fix and hits else ""))
    if args.fix and hits:
        print("The files are clean now; git history still holds the old lines (rewrite it or rotate the key).")


def compact(d, older, strip_read_bodies=False):
    """gzip day files older than `older` (reading tools read .gz too); optionally drop the
    response bodies of reads in them, which are most of the bytes and rarely needed.
    Returns (files, bytes saved)."""
    cutoff = store.parse_since(older)[:10]
    n, saved = 0, 0
    for sub in ("calls", "runs"):
        for f in sorted((d / sub).glob("*.jsonl")):
            if f.stem >= cutoff:
                continue
            before = f.stat().st_size
            lines = f.read_text(encoding="utf-8").splitlines()
            if strip_read_bodies and sub == "calls":
                out = []
                for ln in lines:
                    try:
                        r = json.loads(ln)
                        if r.get("kind") == "read" and r.get("response", {}).get("body"):
                            r["response"]["body"] = {"_stripped": True}
                        out.append(json.dumps(r, ensure_ascii=False))
                    except ValueError:
                        out.append(ln)
                lines = out
            with gzip.open(f.with_suffix(".jsonl.gz"), "wt", encoding="utf-8") as g:
                g.write("\n".join(lines) + "\n")
            f.unlink()
            n += 1
            saved += before - f.with_suffix(".jsonl.gz").stat().st_size
    return n, saved


def cmd_compact(args):
    d = _dir(args)
    n, saved = compact(d, args.older, args.strip_read_bodies)
    print(f"compacted {n} day file(s), {saved / 1e6:.1f} MB smaller. Commit with: docket commit")


def cmd_prune(args):
    """Drop the trail before a date: the day files, and their lines in CHANGES.md. By default the
    writes are kept (they are the point of the trail) and only reads go; --writes-too drops
    everything. The git history still holds the old lines until --rewrite-history (which
    rewrites the trail's own repository, when the trail IS a repository, with git-filter-repo)."""
    d = _dir(args)
    if args.rewrite_history and not args.writes_too:
        sys.exit("docket prune: --rewrite-history removes whole day files from every commit, so it needs --writes-too "
                 "(otherwise the writes you asked to keep would go with them)")
    cutoff = store.parse_since(args.before)[:10]
    files, kept, dropped = [], 0, 0
    for sub in ("calls", "runs"):
        for f in sorted(list((d / sub).glob("*.jsonl")) + list((d / sub).glob("*.jsonl.gz"))):
            if f.name.split(".")[0] >= cutoff:
                continue
            if args.writes_too:
                files.append(f)
                continue
            if sub == "runs":
                continue  # a run line is small and names the writes: keep it with them
            opener = gzip.open if f.suffix == ".gz" else open
            with opener(f, "rt", encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            keep = []
            for ln in lines:
                try:
                    if json.loads(ln).get("kind") == "write":
                        keep.append(ln); kept += 1; continue
                except ValueError:
                    pass
                dropped += 1
            if keep:
                if not args.dry_run:
                    with opener(f, "wt", encoding="utf-8") as fh:
                        fh.write("\n".join(keep) + "\n")
            else:
                files.append(f)
    if args.dry_run:
        print(f"would delete {len(files)} day file(s) before {cutoff}" + ("" if args.writes_too else f", keep {kept} writes, drop {dropped} reads"))
        for f in files:
            print("  ", f.relative_to(d))
        return
    for f in files:
        f.unlink()
    ch = d / "CHANGES.md"
    if args.writes_too and ch.exists():
        lines = ch.read_text(encoding="utf-8").splitlines()
        out = [ln for ln in lines if not (ln.startswith("- ") and ln[2:12] < cutoff)]
        ch.write_text("\n".join(out) + "\n", encoding="utf-8")
        print(f"CHANGES.md: {len(lines) - len(out)} line(s) dropped")
    print(f"deleted {len(files)} day file(s) before {cutoff}" + ("" if args.writes_too else f"; kept {kept} writes, dropped {dropped} reads"))
    msg = f"docket: prune before {cutoff}" + (" (writes too)" if args.writes_too else "")
    print("git:", store.git_commit(d, msg))
    if args.rewrite_history:
        root = store.git_root(d)
        if not root or root != d:
            sys.exit("--rewrite-history only when the trail is its own repository (docket setup makes it one)")
        import shutil
        if not shutil.which("git-filter-repo"):
            sys.exit("--rewrite-history needs git-filter-repo (pipx install git-filter-repo)")
        paths = [f"{sub}/{day}" for sub in ("calls", "runs") for day in _days_before(d, cutoff)]
        # Two passes: the day files go out of every commit, and the period's lines go out of every
        # historical CHANGES.md (they name hosts, paths and reasons too).
        import tempfile
        rules = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        rules.write("".join(f"regex:{pat}==>\n" for pat in _change_line_patterns(d, cutoff))); rules.close()
        cmd = ["git", "-C", str(d), "filter-repo", "--force", "--replace-text", rules.name]
        if paths:
            cmd += ["--invert-paths"] + [a for p in paths for a in ("--path", p)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        os.unlink(rules.name)
        print("history rewritten (day files and CHANGES.md lines)" if r.returncode == 0 else f"history rewrite failed: {r.stderr.strip()[-400:]}")
        subprocess.run(["git", "-C", str(d), "reflog", "expire", "--expire=now", "--all"], check=False)
        subprocess.run(["git", "-C", str(d), "gc", "--prune=now", "-q"], check=False)


def _change_line_patterns(d, cutoff):
    """Regexes (git-filter-repo --replace-text, one per line, multiline) matching every
    `- YYYY-MM-DD …` line of CHANGES.md dated before the cutoff: every earlier year, the earlier
    months of the cutoff year, the earlier days of the cutoff month. Dates, not commit dates: a
    record can predate the commit that first held it."""
    cy, cm, cd = int(cutoff[:4]), int(cutoff[5:7]), int(cutoff[8:10])
    out = []
    if cy > 2000:
        out.append(r"(?m)^- (" + "|".join(str(y) for y in range(2000, cy)) + r")-\d\d-\d\d .*\n")
    if cm > 1:
        out.append(rf"(?m)^- {cy:04d}-(" + "|".join(f"{x:02d}" for x in range(1, cm)) + r")-\d\d .*\n")
    if cd > 1:
        out.append(rf"(?m)^- {cy:04d}-{cm:02d}-(" + "|".join(f"{x:02d}" for x in range(1, cd)) + r") .*\n")
    return out


def _days_before(d, cutoff):
    out = subprocess.run(["git", "-C", str(d), "log", "--all", "--name-only", "--format=", "--", "calls", "runs"],
                         capture_output=True, text=True).stdout.split()
    return sorted({p.split("/", 1)[1] for p in out if "/" in p and p.split("/", 1)[1][:10] < cutoff})


def cmd_migrate(args):
    """Fill project / folder / service into records made before docket 0.2, from their cwd
    (branch is not recoverable). Rewrites the day files in place; idempotent."""
    d = _dir(args)
    cfg = store.load_config(d)
    n_files = n_recs = 0
    for sub in ("calls", "runs"):
        for f in sorted((d / sub).glob("*.jsonl")):
            out, changed = [], False
            for ln in f.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(ln)
                except ValueError:
                    out.append(ln); continue
                if "project" not in r and r.get("cwd"):
                    p = (store.project_of(r["cwd"]) if os.path.isdir(r["cwd"])
                         else {"name": os.path.basename(r["cwd"].rstrip("/")) or None, "folder": None})
                    r["project"], r["folder"] = p["name"], p["folder"]
                    r.setdefault("branch", None)
                    changed = True
                if sub == "calls" and "service" not in r and r.get("host"):
                    r["service"] = store.service_of(r["host"], cfg)
                    changed = True
                n_recs += changed
                out.append(json.dumps(r, ensure_ascii=False) if changed else ln)
            if changed:
                f.write_text("\n".join(out) + "\n", encoding="utf-8")
                n_files += 1
    print(f"migrated {n_files} day file(s). Commit with: docket commit -m 'docket: migrate to 0.2 fields'")


def cmd_status(args):
    d = _dir(args)
    how = ("--dir" if getattr(args, "dir", None) else "DOCKET_DIR" if os.environ.get("DOCKET_DIR")
           else "global (~/.docket)" if d == store.global_dir() else "nearest .docket")
    rows = list(store.iter_records(d, "calls"))
    size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file() and ".git" not in f.parts)
    root = store.git_root(d)
    commits = subprocess.run(["git", "-C", str(root), "rev-list", "--count", "HEAD", "--", str(d)],
                             capture_output=True, text=True).stdout.strip() if root else "0"
    dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", str(d)],
                           capture_output=True, text=True).stdout.count("\n") if root else 0
    print(f"trail:    {d}  ({how})")
    print(f"git:      {root or 'none'}, {commits} commits, {dirty} uncommitted file(s)")
    print(f"size:     {size / 1e6:.1f} MB")
    print(f"calls:    {len(rows)} ({sum(1 for r in rows if r['kind'] == 'write')} writes), "
          f"{len({r.get('project') for r in rows if r.get('project')})} projects")
    if rows:
        print(f"first:    {rows[0]['ts']}\nlast:     {rows[-1]['ts']}")
    print(f"capture:  {'on in this shell' if os.environ.get('DOCKET_RUN') else 'off in this shell'}"
          f"{' (node too)' if NODE_HOOK in os.environ.get('NODE_OPTIONS', '') else ''}")
    none = '(none: docket why "...")'
    print(f"reason:   {os.environ.get('DOCKET_WHY') or store.get_session_why(d) or none}")
    print(f"project:  {store.project_of()['name'] or '(none)'}")


def cmd_mcp(args):
    from . import mcp
    mcp.serve(_dir(args))


def cmd_approvals(args):
    from . import policy
    rows = policy.list_approvals(_dir(args), pending_only=not args.all)
    if args.json:
        _out_json(rows); return
    if not rows:
        print("no pending approvals"); return
    for r in rows:
        ag = r.get("agent") or {}
        print(f"{r['id']}  {r['method']} {r['host']}{r.get('path', '')}  [{r.get('project') or '-'}]  {r.get('why') or '(no reason)'}"
              f"  {ag.get('name') or ''} {ag.get('session', '')[:8]}  {r['state']}")


def cmd_approve(args):
    from . import policy
    for aid in args.ids:
        r = policy.settle(_dir(args), aid, "denied" if args.cmd == "deny" else "approved")
        print(f"{aid}: {r['state'] if r else 'not found'}")


def cmd_policy(args):
    from . import policy
    d = _dir(args)
    pol = policy.load(store.load_config(d))
    if args.json:
        _out_json(pol); return
    if not pol:
        print("no policy: put rules in .docket-policy.json in a repo, or `policy` in the trail's config.json (docket policy --example)")
    else:
        print(f"from {pol.get('_file') or 'config.json'}:")
        for r in pol.get("rules") or []:
            what = "deny" if r.get("deny") else "approve" if r.get("approve") else "allow" if r.get("allow") else "?"
            print(f"  {what:8} {r.get('host', '*'):28} {','.join(r.get('methods') or ['*']):10} project={r.get('project', '*')}"
                  f"{'  unless a reason is set' if r.get('unless_why') else ''}{'  ' + r['note'] if r.get('note') else ''}")
        for b in pol.get("budgets") or []:
            print(f"  budget   {b.get('host', '*'):28} {b.get('writes')} writes per {b.get('per', 'session')}")
        if pol.get("notify"):
            print("  notify   (set)")
    if args.example:
        print(policy.__doc__.split("{", 1)[1].rsplit("}", 1)[0].join("{}"))


def cmd_html(args):
    from . import html as h
    d = _dir(args)
    since = store.parse_since(args.since)
    rows = list(_filter(store.iter_records(d, "calls", since=since), args, since))
    page = h.render(rows, title=args.title or f"Docket · {store.project_of()['name'] or d.name}", since=since)
    out = pathlib.Path(args.out or "docket.html")
    out.write_text(page, encoding="utf-8")
    print(f"wrote {out} ({len(rows)} calls, {sum(1 for r in rows if r['kind'] == 'write')} writes)")


def cmd_commit(args):
    d = _dir(args, required=not args.quiet)
    if d is None:
        return
    days = store.load_config(d).get("compact_after_days") or 0
    if days and not args.no_compact:
        n, saved = compact(d, f"{int(days)}d", strip_read_bodies=True)
        if n and not args.quiet:
            print(f"compacted {n} day file(s) older than {days}d ({saved / 1e6:.1f} MB smaller)")
    out = store.git_commit(d, args.message or "docket: record API calls")
    if not args.quiet:
        print(out)


# ---- main ------------------------------------------------------------------------------------
def _filters(a, since):
    a.add_argument("--since", default=since, help="10m, 12h, 7d, 2w or a date")
    a.add_argument("--project", "-p", help="project name (glob)")
    a.add_argument("--here", action="store_true", help="only the project of the current folder")
    a.add_argument("--service", help="e.g. 'Google Ads', 'Stripe' (substring)")
    a.add_argument("--host"); a.add_argument("--method"); a.add_argument("--run")
    a.add_argument("--status", help="200, 4xx, BLOCKED ...")
    a.add_argument("--why", help="substring of the reason")
    a.add_argument("--secret", help="calls that used this credential (env var name)")
    a.add_argument("--grep", help="substring anywhere in the record")
    a.add_argument("--json", action="store_true")


def main(argv=None):
    p = argparse.ArgumentParser(prog="docket", description="git-friendly record of every API call, from every folder, with the reason",
        epilog=textwrap.dedent("""\
        start:
          docket setup --write                          # one trail for every folder on this machine
          docket why "raise Google Ads bids to $14"     # reason for this session's calls
          docket run --why "..." -- python x.py         # or per command; --dry blocks writes
        read:
          docket log --since 7d --writes --here         # this project's changes
          docket projects | docket secrets | docket runs
          docket show 20260908-170312-a1b2c3
          docket report --since 30d > changes.md"""), formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", help="trail directory (default: $DOCKET_DIR, nearest .docket, else ~/.docket)")
    p.add_argument("--version", action="version", version=__version__)
    sp = p.add_subparsers(dest="cmd", required=True)
    a = sp.add_parser("setup", help="one trail for the whole machine + the shell block that turns capture on everywhere")
    a.add_argument("path", nargs="?", help="trail location (default ~/.docket, or the one in use)")
    a.add_argument("--shell", choices=["zsh", "bash"], default="zsh")
    a.add_argument("--profile", help="file to write the block to (default ~/.zshenv / ~/.bashrc)")
    a.add_argument("--when", choices=["agents", "always"], default="agents",
                   help="agents: only in AI-agent shells (Claude Code, Codex, Cursor) or with DOCKET_ON=1; always: every shell")
    a.add_argument("--write", action="store_true", help="write the block into the profile"); a.set_defaults(f=cmd_setup)
    a = sp.add_parser("init", help="a trail for one repo (.docket here)"); a.add_argument("path", nargs="?"); a.set_defaults(f=cmd_init)
    a = sp.add_parser("link", help="make this folder log into another repo's trail"); a.add_argument("target"); a.set_defaults(f=cmd_link)
    a = sp.add_parser("run", help="run a command and record every API call it makes")
    a.add_argument("--why", "-w", help="one line on what you are changing and why (goes on every record)")
    a.add_argument("--dry", action="store_true", help="record and BLOCK every write; reads go through")
    a.add_argument("--commit", "-c", action="store_true", help="git commit the trail after the run")
    a.add_argument("command", nargs=argparse.REMAINDER); a.set_defaults(f=cmd_run)
    a = sp.add_parser("why", help="set the reason for this session's calls (docket why \"...\"), or show it")
    a.add_argument("text", nargs="*"); a.add_argument("--clear", action="store_true")
    a.add_argument("--from-claude-hook", action="store_true", help=argparse.SUPPRESS); a.set_defaults(f=cmd_why)
    a = sp.add_parser("hook", help="agent hooks: reason from each request, commit after each turn")
    a.add_argument("agent", choices=["claude"]); a.set_defaults(f=cmd_hook)
    a = sp.add_parser("log", help="timeline of calls"); _filters(a, "7d")
    a.add_argument("--writes", action="store_true"); a.add_argument("--reads", action="store_true")
    a.add_argument("--limit", type=int, default=200); a.add_argument("--follow", "-f", action="store_true", help="keep printing new calls")
    a.set_defaults(f=cmd_log)
    a = sp.add_parser("show", help="full record of one call"); a.add_argument("id"); a.set_defaults(f=cmd_show)
    a = sp.add_parser("report", help="markdown summary of writes, by project and reason"); _filters(a, "30d")
    a.add_argument("--width", type=int, default=240); a.add_argument("--per-group", type=int, default=12); a.set_defaults(f=cmd_report)
    a = sp.add_parser("projects", help="which projects called which services"); a.add_argument("--since", default="30d")
    a.add_argument("--json", action="store_true"); a.set_defaults(f=cmd_projects)
    a = sp.add_parser("secrets", help="which credential (by env var name) was used where, and its versions")
    a.add_argument("--since", default="90d"); a.add_argument("--json", action="store_true"); a.set_defaults(f=cmd_secrets)
    a = sp.add_parser("runs", help="commands run, with project, reason and exit code"); a.add_argument("--since", default="7d")
    a.add_argument("--project", "-p"); a.add_argument("--limit", type=int, default=100); a.add_argument("--json", action="store_true"); a.set_defaults(f=cmd_runs)
    a = sp.add_parser("noise", help="hosts filling the trail with reads"); a.add_argument("--since", default="30d")
    a.add_argument("--limit", type=int, default=20); a.set_defaults(f=cmd_noise)
    a = sp.add_parser("ignore", help="stop recording reads to these hosts (--all: every call)"); a.add_argument("hosts", nargs="+")
    a.add_argument("--all", action="store_true"); a.set_defaults(f=cmd_ignore)
    a = sp.add_parser("guard", help="refuse writes to these hosts unless a reason is set"); a.add_argument("hosts", nargs="*")
    a.add_argument("--remove", action="store_true"); a.set_defaults(f=cmd_guard)
    a = sp.add_parser("scan", help="find anything credential-shaped left in the trail"); a.add_argument("--fix", action="store_true")
    a.set_defaults(f=cmd_scan)
    a = sp.add_parser("compact", help="gzip old days (and optionally drop read response bodies)"); a.add_argument("--older", default="14d")
    a.add_argument("--strip-read-bodies", action="store_true"); a.set_defaults(f=cmd_compact)
    a = sp.add_parser("prune", help="drop the trail before a date (reads by default; --writes-too for everything)")
    a.add_argument("--before", required=True, help="a date, or 30d / 8w")
    a.add_argument("--writes-too", action="store_true", help="drop writes and their CHANGES.md lines as well")
    a.add_argument("--rewrite-history", action="store_true", help="also remove the days from the trail's git history (git-filter-repo)")
    a.add_argument("--dry-run", action="store_true"); a.set_defaults(f=cmd_prune)
    a = sp.add_parser("unhook", help="remove the docket_hook.pth files planted in site-packages")
    a.add_argument("sites", nargs="*", help="extra site-packages directories")
    a.add_argument("--find", action="store_true", help="list every one under ~ (with --remove: remove them)")
    a.add_argument("--remove", action="store_true"); a.set_defaults(f=cmd_unhook)
    a = sp.add_parser("migrate", help="add project/folder/service to records made before 0.2"); a.set_defaults(f=cmd_migrate)
    a = sp.add_parser("status", help="where the trail is, its size, and what this shell records"); a.set_defaults(f=cmd_status)
    a = sp.add_parser("env", help="print the shell exports (setup --write installs them)"); a.set_defaults(f=cmd_env)
    a = sp.add_parser("mcp", help="serve the trail to an agent over MCP (stdio)"); a.set_defaults(f=cmd_mcp)
    a = sp.add_parser("approvals", help="writes waiting for a person (policy `approve` rules)"); a.add_argument("--all", action="store_true")
    a.add_argument("--json", action="store_true"); a.set_defaults(f=cmd_approvals)
    a = sp.add_parser("approve", help="let a waiting write go"); a.add_argument("ids", nargs="+"); a.set_defaults(f=cmd_approve)
    a = sp.add_parser("deny", help="refuse a waiting write"); a.add_argument("ids", nargs="+"); a.set_defaults(f=cmd_approve)
    a = sp.add_parser("policy", help="the rules in force here: deny, approve, budgets (.docket-policy.json / config)")
    a.add_argument("--json", action="store_true"); a.add_argument("--example", action="store_true"); a.set_defaults(f=cmd_policy)
    a = sp.add_parser("html", help="write the timeline as one self-contained HTML page"); _filters(a, "30d")
    a.add_argument("--out", "-o"); a.add_argument("--title"); a.set_defaults(f=cmd_html)
    a = sp.add_parser("commit", help="git commit whatever the trail has recorded (compacts old days first; never pushes)")
    a.add_argument("-m", "--message"); a.add_argument("--no-compact", action="store_true")
    a.add_argument("-q", "--quiet", action="store_true"); a.set_defaults(f=cmd_commit)
    args = p.parse_args(argv)
    if args.cmd == "run" and args.command and args.command[0] == "--":
        args.command = args.command[1:]
    args.f(args)


if __name__ == "__main__":
    main()
