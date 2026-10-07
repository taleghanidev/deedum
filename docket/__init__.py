"""docket — a git-friendly audit trail of every API call a command makes.

Zero code changes: `docket run --why "raise bids" -- python script.py` records each HTTP
(requests/httpx/urllib) and gRPC call the process makes into a directory that lives in git.
`docket log` / `show` / `report` read it back for humans and AI agents.
"""
__version__ = "0.2.0"
from .recorder import install, Recorder  # noqa: F401
