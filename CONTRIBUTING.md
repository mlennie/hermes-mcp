# Contributing to hermes-mcp

Thanks for considering a contribution. This project is small on purpose — a focused bridge from Claude to Hermes Agent. Patches that keep it small are very welcome.

## Quick start

```bash
git clone https://github.com/mlennie/hermes-mcp.git
cd hermes-mcp
uv venv .venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[dev]"

# Run the test + lint suite the same way CI does
ruff check .
ruff format --check .
mypy src/
pytest
```

## How to propose changes

1. Open an issue first for anything beyond a typo or doc fix. Especially before adding new MCP tools or new dependencies — the project deliberately keeps its surface tiny.
2. Branch off `main`, make focused commits, add tests for new behavior.
3. Keep `CHANGELOG.md` updated under the `Unreleased` section.
4. Open a PR. CI must pass before review.

## Things we welcome

- Bug fixes with regression tests.
- Documentation improvements — especially screenshots of the Claude Desktop / mobile connector setup.
- Hardening: extra tests around the OAuth provider, the gateway HTTP client, timeouts, log redaction.
- Improvements to the doctor self-checks.
- Translations / i18n of the README (in a `docs/i18n/` subdir).

## Things we are cautious about

- **New MCP tools.** The plan is to start with `hermes_ask` only and only add specialized tools (`hermes_schedule_cron`, etc.) when there's a clear, repeated friction in real-world use.
- **New dependencies.** Each one is supply-chain surface. Justify in the PR description.
- **Telemetry / analytics.** No, by policy. Don't propose this.
- **Authentication changes.** OAuth 2.1 is the auth model. Changes that touch `oauth.py`, `RequireAuthMiddleware` wiring, or the exact redirect-URI allowlist need a strong rationale and tests covering the security invariants.

## Code style

- `ruff` for lint + format. Just run `ruff format` before committing.
- `mypy --strict` clean.
- Short docstrings. Explain *why*, not *what* — the code says what.

## Reporting security issues

See [SECURITY.md](SECURITY.md). Do not open public issues for vulnerabilities.

## Release process

(Maintainer notes; here for transparency.)

1. Bump `__version__` in `src/hermes_mcp/__init__.py` and the version in `pyproject.toml`.
2. Move `Unreleased` entries in `CHANGELOG.md` under the new version heading with today's date.
3. Tag: `git tag v0.X.Y && git push --tags`.
4. The `release` workflow publishes to PyPI via trusted publishing.
