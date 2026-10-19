# Installed by `deedum run` into a virtualenv's site-packages next to deedum_hook.pth.
# Does nothing unless the process was started under `deedum run` (DEEDUM_RUN is set), and then
# loads deedum only from the checkout that planted this file (_EXPECTED, filled in at install):
# an environment variable alone must not be able to put code into every Python on the machine.
import os, sys
_EXPECTED = None  # replaced with the package path when the hook is planted
if os.environ.get("DEEDUM_RUN"):
    _pkg = os.environ.get("DEEDUM_PKG")
    if _pkg and _EXPECTED and os.path.realpath(_pkg) == os.path.realpath(_EXPECTED):
        if _pkg not in sys.path:
            sys.path.insert(0, _pkg)
        try:
            import deedum
            deedum.install()
        except Exception as _e:
            sys.stderr.write(f"[deedum] hook failed in this process: {_e}\n")
