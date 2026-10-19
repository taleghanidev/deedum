"""deedum — a git-friendly audit trail of every API call a command makes.

Zero code changes: `deedum run --why "raise bids" -- python script.py` records each HTTP
(requests/httpx/urllib) and gRPC call the process makes into a directory that lives in git.
`deedum log` / `show` / `report` read it back for humans and AI agents.
"""
__version__ = "0.3.0"
from .recorder import install, Recorder  # noqa: F401
