# Installed by `docket run` into a virtualenv's site-packages next to docket_hook.pth.
# Does nothing unless the process was started under `docket run` (DOCKET_RUN is set).
import os, sys
if os.environ.get("DOCKET_RUN"):
    _pkg = os.environ.get("DOCKET_PKG")
    if _pkg and _pkg not in sys.path:
        sys.path.insert(0, _pkg)
    try:
        import docket
        docket.install()
    except Exception as _e:
        sys.stderr.write(f"[docket] hook failed in this process: {_e}\n")
