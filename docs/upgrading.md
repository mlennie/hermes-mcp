# Upgrade an existing Town / Hermes MCP connection

This update prevents long Hermes requests from freezing other MCP calls and saves
job results across bridge restarts. Your Town skill, gateway/session mapping,
OAuth credentials, and Hermes model settings can stay as they are.

## Before restarting

Let active work finish and record any answers you need from the old bridge.
The old version's in-memory job list cannot be migrated or recovered after exit.
Save your existing environment file and deployment configuration. Do not replace
your credentials, public hostname, gateway URL, or existing Hermes service.

## Docker / Hostinger installation

1. Update the bridge source to the new revision from the repository you installed.
2. Compare your Compose file with the updated example in `deploy/`. Add the
   `hermes-mcp-jobs` named volume mounted at `/var/lib/hermes-mcp` to the **bridge**
   service. Set `HERMES_MCP_JOB_STORE_PATH=/var/lib/hermes-mcp/jobs.sqlite3`.
   Both supplied Compose examples include these settings.
3. Rebuild and recreate the bridge service using your existing Compose files.
   Do not recreate unrelated services or delete volumes. The image prepares the
   new volume directory for its non-root service account; a custom bind mount
   must already be writable by that account and private to it.
4. Keep this volume on future upgrades. Deleting it deletes saved job results.

If you run the bridge from a shared virtual environment instead of the provided
image, update that installation and point `HERMES_MCP_JOB_STORE_PATH` at an
absolute path on your existing persistent mount. Do not assume a container's
ordinary writable filesystem survives recreation.

## Local or systemd installation

Update the package using the same installer and repository source you originally
used. For local use the default database is
`~/.local/state/hermes-mcp/jobs.sqlite3`.

For systemd, apply the new `Environment=HERMES_MCP_JOB_STORE_PATH=...` line from
`deploy/hermes-mcp.service`, reload the user service definitions, and restart the
bridge. It selects `~/.config/hermes-mcp/jobs.sqlite3`, which is writable under
the supplied service's sandbox. An explicit environment-file override must also
be allowed by that sandbox. Keep one bridge process per database.

## Confirm it worked

- Reconnect Town if its OAuth access token was invalidated by the restart.
  Static bearer-token configuration and OAuth client credentials are unchanged.
- Ask a harmless question with `async_mode=true`. Save its job ID, and wait for
  `hermes_check` to return a completed answer.
- When no important work is running, restart the bridge and check the same job ID.
  The completed answer should still be available.
- During a long request, other MCP status checks should remain responsive.

Results expire approximately 24 hours after reaching a terminal state. An
explicit `hermes_reset` also removes them. Neither cancellation nor reset stops
underlying Hermes work. An interrupted request is retained as failed with an
**unconfirmed outcome**: consult the original session and artifacts instead of
submitting it again automatically.

The completion boundary is unchanged: a completed bridge job confirms a gateway
answer, not completion of all downstream work. OAuth token persistence is separate
from job persistence and is not part of this update.

## Returning to an older version

Restore your saved package revision and deployment configuration, preserving the
job volume. An older bridge may not read the database and will resume its old
in-memory behavior; its old jobs are not restored by rolling back. Do not copy a
live SQLite database alone for backup: stop the bridge first or use SQLite's
backup facilities so committed WAL data is included.
