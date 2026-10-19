# Contributing

Docket is small on purpose: stdlib Python, one Node file, no dependencies. Keep it that way.

## Setting up

```sh
git clone https://github.com/taleghanidev/docket && cd docket
pip install -e ".[test]" requests      # requests is optional; one test uses it when present
python -m pytest -q tests              # or: python3 tests/test_docket.py
```

The tests start a local HTTP server and run real `docket run` subprocesses for Python, Node and
curl, so they need `node` on PATH for the Node cases (they skip when it is missing).

## What a change needs

- A test. Every hook and every redaction rule has one; a bug fix adds the case that was missed.
- The redaction invariant: no test may leave a secret value in the trail. If you add a transport
  or a field, run the secret through it in a test and assert it is not in the files.
- Both runtimes. A change to classification, redaction or the record shape goes into
  `docket/recorder.py` **and** `docket/shim/node_hook.cjs`; they must write the same records.
- A line in `CHANGELOG.md` under the next version.
- No new dependencies. A hook attaches lazily to a library only if the program already imports it.

## Adding a hook or a shim

- A Python library: a `_hook_<lib>(rec)` in `recorder.py` that returns `False` when the library is
  not importable, records through `rec.record({...})` and gates through `rec.gate(...)`, plus its
  name in the import hook list in `install()`.
- A binary: its name in `EXEC_SHIMS` (`cli.py`) and, if it has read-only subcommands, a pattern
  in `READS` (`shim/_exec`). Writes are the default; a wrong "read" is the worse mistake.
- A service name: `docket/shim/services.json` (shared by both runtimes).

## Security issues

See `SECURITY.md`. Please do not open a public issue for anything that could expose a trail.

## Licence

By contributing you agree that your contribution is licensed under the MIT licence in `LICENSE`,
the same terms as the rest of the project. There is no CLA.
