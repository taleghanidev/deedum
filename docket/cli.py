"""docket command line."""
import os, sys, json, argparse, subprocess, pathlib, uuid, textwrap, collections, shlex
from . import store, __version__

PKG_DIR = str(pathlib.Path(__file__).resolve().parent.parent)
SHIM_DIR = str(pathlib.Path(__file__).resolve().parent / "shim")

def _dir(args, required=True):
    d = store.find_dir(getattr(args, "dir", None))
    if d is None and required:
        sys.exit("docket: no trail directory found. Run `docket init` in the repo you want the "
                 "history kept in, or set DOCKET_DIR.")
    return d

def cmd_init(args):
    target = pathlib.Path(args.path or ".").resolve() / store.DIRNAME
    store.init_dir(target)
    root = store.git_root(target)
    print(f"initialised {target}")
    print("inside git repo:", root or "NO — `git init` here if you want history kept")
    print("next: docket run --why \"what you are about to change\" -- <command>")

def cmd_link(args):
    """Write a `.docket` pointer file in cwd so runs from here log into another repo's trail."""
    target = pathlib.Path(os.path.expanduser(args.target)).resolve()
    if target.name != store.DIRNAME:
        target = target / store.DIRNAME
    if not target.is_dir():
        sys.exit(f"{target} is not an initialised trail (run `docket init` there first)")
    pathlib.Path(store.DIRNAME).write_text(str(target) + "\n")
    print(f".docket -> {target}")

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
    env["PYTHONPATH"] = SHIM_DIR + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PATH"] = SHIM_DIR + os.pathsep + env.get("PATH", "")
    args.command = _merge_pythonpath(args.command)
    _install_pth(args.command)
    started = store.now_iso()
    try:
        code = subprocess.call(args.command, env=env)
    except KeyboardInterrupt:
        code = 130
    calls = [r for r in store.iter_records(d, "calls", since=started[:10]) if r.get("run") == run_id]
    writes = [r for r in calls if r["kind"] == "write"]
    hosts = sorted({r["host"] for r in calls})
    print(f"\n[docket] run {run_id}: {len(calls)} calls ({len(writes)} writes) to {', '.join(hosts) or 'nothing'}; exit {code}")
    for w in writes:
        print(f"[docket]   {w['method']} {w['host']}{w['path']} -> {w['status'] if w['status'] is not None else w['error']}  `{w['id']}`")
    cfg = store.load_config(d)
    if (args.commit or cfg.get("auto_commit")) and calls:
        msg = f"docket: {args.why or ' '.join(args.command)[:60]}"
        body = f"\n\nrun {run_id}: {len(calls)} calls, {len(writes)} writes\n" + "\n".join(
            f"- {w['method']} {w['host']}{w['path']} -> {w['status'] or w['error']} ({w['id']})" for w in writes)
        print("[docket] git:", store.git_commit(d, msg + body))
    sys.exit(code)

def _merge_pythonpath(cmd):
    """`env PYTHONPATH=. python x.py` would replace our hook path: prepend the shim to it."""
    out = []
    for tok in cmd:
        if tok.startswith("PYTHONPATH="):
            tok = "PYTHONPATH=" + SHIM_DIR + os.pathsep + tok[len("PYTHONPATH="):]
        out.append(tok)
    return out

def _install_pth(cmd):
    """If the command runs a Python interpreter, drop an env-gated .pth hook into its site-packages
    so the hooks load even when the script rewrites PYTHONPATH or spawns clean subprocesses."""
    import shutil
    exe = None
    for tok in cmd:
        if tok == "env" or "=" in tok:
            continue
        exe = tok; break
    if not exe:
        return
    base = os.path.basename(exe)
    if not base.startswith("python"):
        return
    path = shutil.which(exe) if os.sep not in exe else exe
    if not path:
        return
    try:
        site = subprocess.run([path, "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"],
                              capture_output=True, text=True, timeout=20).stdout.strip()
        if not site or not os.path.isdir(site):
            return
        hook = pathlib.Path(site) / "docket_hook.py"
        pth = pathlib.Path(site) / "docket_hook.pth"
        src = pathlib.Path(SHIM_DIR) / "docket_hook.py"
        if not hook.exists() or hook.read_text() != src.read_text():
            hook.write_text(src.read_text())
        if not pth.exists():
            pth.write_text("import docket_hook\n")
            print(f"[docket] hook installed in {site}")
    except (PermissionError, OSError, subprocess.TimeoutExpired):
        pass

def cmd_env(args):
    """Print shell exports that turn on capture for every Python and curl started from this shell.
    Use: eval "$(docket env)"  — typically in ~/.zshrc for Claude Code sessions."""
    d = _dir(args)
    q = shlex.quote
    print(f"export DOCKET_DIR={q(str(d))}")
    print(f"export DOCKET_RUN=always")
    print(f"export DOCKET_PKG={q(PKG_DIR)}")
    print(f'export PYTHONPATH={q(SHIM_DIR)}"${{PYTHONPATH:+:$PYTHONPATH}}"')
    print(f'export PATH={q(SHIM_DIR)}:"$PATH"')

def cmd_commit(args):
    d = _dir(args)
    print(store.git_commit(d, args.message or "docket: record API calls"))

def _fmt_row(r):
    st = r["status"] if r.get("status") is not None else (r.get("error") or "")[:24]
    why = f"  why: {r['why']}" if r.get("why") else ""
    return f"{r['ts'][:16].replace('T',' ')}  {r['kind']:5} {r['method']:6} {r['host']}{r['path']}  {st}{why}  `{r['id']}`"

def cmd_log(args):
    d = _dir(args)
    since = store.parse_since(args.since)
    rows = [r for r in store.iter_records(d, "calls", since=since)
            if (not since or r["ts"] >= since)
            and (not args.writes or r["kind"] == "write")
            and (not args.host or args.host in (r.get("host") or ""))
            and (not args.grep or args.grep.lower() in json.dumps(r).lower())]
    if not rows:
        print("no calls match"); return
    for r in rows[-args.limit:]:
        print(_fmt_row(r))
    print(f"-- {len(rows)} calls ({sum(1 for r in rows if r['kind']=='write')} writes)")

def cmd_show(args):
    d = _dir(args)
    for r in store.iter_records(d, "calls", since=args.id[:4] + "-" + args.id[4:6] + "-" + args.id[6:8]):
        if r["id"] == args.id:
            print(json.dumps(r, indent=2, ensure_ascii=False)); return
    sys.exit(f"no record {args.id}")

def cmd_report(args):
    d = _dir(args)
    since = store.parse_since(args.since)
    rows = [r for r in store.iter_records(d, "calls", since=since) if (not since or r["ts"] >= since)]
    writes = [r for r in rows if r["kind"] == "write"]
    print(f"# API changes since {since or 'the beginning'}\n")
    print(f"{len(rows)} calls, {len(writes)} writes, {len({r['run'] for r in rows})} runs, hosts: "
          f"{', '.join(sorted({r['host'] for r in rows}))}\n")
    by_day = collections.OrderedDict()
    for w in writes:
        by_day.setdefault(w["ts"][:10], []).append(w)
    for day, ws in by_day.items():
        print(f"## {day}\n")
        by_why = collections.OrderedDict()
        for w in ws:
            by_why.setdefault(w.get("why") or "(no reason given)", []).append(w)
        for why, group in by_why.items():
            print(f"**{why}** — {len(group)} write(s)\n")
            for w in group:
                body = (w.get("request") or {}).get("body") or {}
                summary = ""
                j = body.get("_json") if isinstance(body, dict) else None
                if j is not None:
                    summary = json.dumps(j, ensure_ascii=False)
                    summary = summary[:args.width] + ("…" if len(summary) > args.width else "")
                st = w["status"] if w.get("status") is not None else w.get("error")
                print(f"- {w['ts'][11:16]} {w['method']} {w['host']}{w['path']} → {st} `{w['id']}`")
                if summary:
                    print(f"  `{summary}`")
            print()

def cmd_status(args):
    d = _dir(args)
    rows = list(store.iter_records(d, "calls"))
    runs = list(store.iter_records(d, "runs"))
    print(f"trail: {d}")
    print(f"git repo: {store.git_root(d) or 'none'}")
    print(f"calls: {len(rows)} ({sum(1 for r in rows if r['kind']=='write')} writes) in {len(runs)} runs")
    if rows:
        print(f"first: {rows[0]['ts']}  last: {rows[-1]['ts']}")

def main(argv=None):
    p = argparse.ArgumentParser(prog="docket", description="git-friendly audit trail of API calls",
        epilog=textwrap.dedent("""\
        examples:
          docket init                                   # in the repo that should keep the history
          docket run --why "raise the brand campaign bids" -- python raise_bids.py --apply
          docket log --since 7d --writes
          docket show 20260908-170312-a1b2c3
          docket report --since 30d > changes.md"""), formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", help="trail directory (default: $DOCKET_DIR or nearest .docket)")
    p.add_argument("--version", action="version", version=__version__)
    sp = p.add_subparsers(dest="cmd", required=True)
    a = sp.add_parser("init", help="create .docket here (or at PATH)"); a.add_argument("path", nargs="?"); a.set_defaults(f=cmd_init)
    a = sp.add_parser("link", help="make runs from this directory log into another repo's .docket"); a.add_argument("target"); a.set_defaults(f=cmd_link)
    a = sp.add_parser("run", help="run a command and record every API call it makes")
    a.add_argument("--why", "-w", help="one line on what you are changing and why (goes on every record)")
    a.add_argument("--commit", "-c", action="store_true", help="git commit the trail after the run")
    a.add_argument("command", nargs=argparse.REMAINDER); a.set_defaults(f=cmd_run)
    a = sp.add_parser("log", help="timeline of calls"); a.add_argument("--since", default="7d"); a.add_argument("--writes", action="store_true")
    a.add_argument("--host"); a.add_argument("--grep"); a.add_argument("--limit", type=int, default=200); a.set_defaults(f=cmd_log)
    a = sp.add_parser("show", help="full record of one call"); a.add_argument("id"); a.set_defaults(f=cmd_show)
    a = sp.add_parser("report", help="markdown summary of writes"); a.add_argument("--since", default="30d"); a.add_argument("--width", type=int, default=240); a.set_defaults(f=cmd_report)
    a = sp.add_parser("status", help="where the trail is and how much is in it"); a.set_defaults(f=cmd_status)
    a = sp.add_parser("env", help='always-on mode: eval "$(docket env)" in a shell profile'); a.set_defaults(f=cmd_env)
    a = sp.add_parser("commit", help="git commit whatever the trail has recorded"); a.add_argument("-m", "--message"); a.set_defaults(f=cmd_commit)
    args = p.parse_args(argv)
    if args.cmd == "run" and args.command and args.command[0] == "--":
        args.command = args.command[1:]
    args.f(args)

if __name__ == "__main__":
    main()
