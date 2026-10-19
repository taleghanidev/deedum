# Deedum

Read `AGENTS.md` first: it says how a coding agent uses deedum (reason before a write, read the
trail before answering what changed, rehearse with `--dry`, never work around a refusal) and how
to work on this repository (`CONTRIBUTING.md`, `SECURITY.md`).

Tests: `python3 tests/test_deedum.py` (or `python -m pytest -q tests`). They start a local HTTP
server and run real `deedum run` subprocesses for Python, Node, curl and the binary shims.
