# Injected by `docket run` via PYTHONPATH. Installs the hooks in every Python process it starts.
import os, sys
_pkg = os.environ.get("DOCKET_PKG")
if _pkg and _pkg not in sys.path:
    sys.path.insert(0, _pkg)
try:
    import docket
    docket.install()
except Exception as _e:
    sys.stderr.write(f"[docket] not installed in this process: {_e}\n")
# chain any sitecustomize that this one shadowed
_me = os.path.dirname(os.path.abspath(__file__))
_orig = [p for p in sys.path if p and os.path.abspath(p) != _me]
import importlib.util as _u
for _p in _orig:
    _f = os.path.join(_p, "sitecustomize.py")
    if os.path.isfile(_f):
        _spec = _u.spec_from_file_location("sitecustomize_chained", _f)
        _m = _u.module_from_spec(_spec); _spec.loader.exec_module(_m); break
