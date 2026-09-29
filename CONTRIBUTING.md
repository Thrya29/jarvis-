# Contributing

## Setup

```powershell
python -m pip install --user uv
uv sync
```

## Workflow

- One branch and pull request per milestone or feature, targeting `main`.
- CI (lint, format, strict mypy, tests, frozen-build smoke test) must pass before merge.
- Keep `uv.lock` committed; update it with `uv lock` when changing dependencies.

## Standards

- Python 3.12, fully typed (`mypy --strict`).
- `ruff` for lint and format; line length 100.
- Every tool that acts on the machine goes through the safety layer and writes to the audit log.
- Tests must not touch the real user profile: the test suite isolates `JARVIS_HOME` and the keyring.

## Releasing

1. Bump `version` in `pyproject.toml`, merge to `main`.
2. Tag: `git tag v0.2.0 && git push origin v0.2.0`.
3. The Release workflow verifies the tag, builds the installer, and publishes it with SHA-256 checksums.
