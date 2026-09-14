## What this does

<!-- One paragraph. What changes and why. Link the issue if there is one (Fixes #NNN). -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Security fix
- [ ] Refactor (no behavior change)
- [ ] Docs / config only

## Security impact

<!-- hermes-mcp is a thin auth + HTTP gateway wrapper. Before merging, confirm:
  - Does this change the authentication or bearer-token handling? If yes, describe.
  - Does this change requests to the configured Hermes gateway? If yes, describe.
  - Does this add logging of prompt content above DEBUG level? It must not.
  - Does this add any outbound network call beyond the configured Hermes gateway? It must not (no telemetry policy).
If none of the above apply, write "None." -->

## Testing done

- [ ] `ruff check .` passes
- [ ] `ruff format --check .` passes
- [ ] `mypy src/` passes
- [ ] `pytest` passes
- [ ] Manually tested against a real Hermes installation *(required if touching `hermes_client.py` or `server.py`)*

## Checklist

- [ ] `CHANGELOG.md` updated under `Unreleased`
- [ ] Breaking changes (env var renames, CLI flag changes) noted in `CHANGELOG.md`
