## What

<!-- What does this change and why? -->

## How verified

- [ ] `uv run ruff check . && uv run ruff format --check .`
- [ ] `uv run mypy`
- [ ] `uv run pytest`
- [ ] Manually exercised on Windows (describe below)

## Safety

- [ ] No new tool can act on the machine without going through the safety layer
- [ ] No secrets or user content written to logs unredacted
