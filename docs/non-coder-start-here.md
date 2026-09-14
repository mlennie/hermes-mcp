# Start here if you do not write code

`hermes-mcp` is a connector. It lets an assistant such as Town send a request to a Hermes
Agent that runs on your server, and then receive Hermes's reply.

```text
You -> Town -> hermes-mcp -> Hermes Agent -> workers and tools
```

It does not replace Town or Hermes. It provides the secure doorway between them.

## What you need

- A working Hermes Agent installation. Hostinger's managed Hermes application is suitable.
- A public web address for the connector, such as `hermes.example.com`.
- A Town account that can add a custom MCP server.
- One person or agent comfortable making the one-time Docker configuration change.

The server setup still involves Docker and credentials. You should not need to understand
the code, but you should know which Hermes installation and Town assistant you are
connecting.

## The shortest safe setup path

1. Confirm that Hermes Agent itself works before adding this connector.
2. Add `hermes-mcp` as a separate container. On Hostinger, use the
   [Hostinger sidecar example](../deploy/docker-compose.hostinger-sidecar.yml) together with
   the existing Hermes Compose application.
3. Give the connector a public HTTPS address and keep the Traefik routing labels on the
   `hermes-mcp` container.
4. In Town, open **Settings -> MCP -> Add Server**. Enter the connector's public address
   followed by `/mcp`, then complete the sign-in flow.
5. Enable the connector only for the Town routine that needs Hermes.
6. Run the read-only test below before sending implementation work.

The full server procedure is in the
[Town and Hostinger deployment guide](town-hostinger.md).

## First test: read only

Replace the bracketed text and send this through the Town routine:

```text
Give me the current status of <PROJECT_NAME> and tell me whether anything requires me.
Consult the project's current records rather than relying only on conversational memory.
This is a read-only check. Do not create or change a task, worker, session, branch, code,
deployment, or release.
```

A successful test has four signs:

- Town reaches the intended Hermes Project Lead.
- The reply names the records it consulted.
- Nothing new is created.
- The reply clearly says whether you have an action.

## Second test: one bounded instruction

After the read-only test passes, send one small instruction that you have explicitly
authorized. Ask for a draft, status check, or other reversible result first. Confirm that
only one expected task or worker is created.

For ongoing use, copy and adapt the
[durable Hermes Project Lead routine template](town-routine-template.md). It keeps one
stable Project Lead session, separates ideas from permission to implement, checks for
duplicate work, and makes required user action explicit.

## What “completed” means

The connector can finish delivering a request while a Hermes worker is still working.
For that reason, asynchronous responses include:

```text
completion_scope: gateway_response
```

This means, “Hermes replied to the connector.” It does not by itself mean that code,
testing, deployment, or a release is finished. Town should ask the same durable Hermes
session for current downstream status and consult the system that owns the facts.

## Common problems

| What you see | What it usually means | What to check |
|---|---|---|
| `401` at `/mcp` before signing in | The protected connector is reachable | Complete OAuth in Town |
| `404` | The public router did not match the connector | Hostname and router rule |
| `502` | Traefik cannot reach the connector or selected the wrong service | Put the router labels on `hermes-mcp` and verify port 8765 |
| Town reaches the wrong project | The routine used the wrong or changing session ID | Use one stable, role-based `session_id` |
| Town says work is done too early | It treated the gateway reply as downstream completion | Follow up through the same session and check the task, CI, or deployment system |

## Plain-language glossary

- **MCP:** A standard that lets one AI application use tools provided by another service.
- **Connector or bridge:** `hermes-mcp`, the doorway between Town and Hermes.
- **Sidecar:** A small companion container that runs beside Hermes on the same server.
- **OAuth:** The browser sign-in flow Town uses to connect securely.
- **Durable session:** One stable Hermes conversation identity reused for the same role.
- **Gateway response:** Hermes has answered the connector, although separately delegated
  work may still be running.
