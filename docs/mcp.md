# Atlas MCP server

Atlas is a live context store for browser agents, backed by HydraDB. It keeps a graph of the
sites your agent works on: pages, the actions that connect them, the goals those actions
achieve, reusable skills, and the entities (order ids, user names, ...) resolved along the way.
Instead of preloading every tool and every page, your agent asks Atlas for what the current
task needs: the skills that match a goal, the known route from the page it is on, or where an
entity was last seen. Every step it reports makes the map better for the next run.

The hosted endpoint is

```
https://atlas.yashdynamo.fun/mcp
```

It speaks MCP over streamable HTTP and needs a key on every request: a bearer header on `/mcp`,
or the path `/mcp/k/<key>`.

## Keys

| Key | Where it comes from | Namespace | Can write |
| --- | --- | --- | --- |
| Demo key | `GET /v1/mcp-info` (`demo_key`) | the shared, pre-mapped `demo` map | no |
| Private key | `POST /v1/keys` | your own, empty map | yes |

- The **demo key** is public. It plans, finds skills and recalls entities on the demo map,
  which is already mapped. `atlas_report` (and every other write) is refused with a
  `read-only` error, and planning from an unknown page records nothing.
- A **private key** gets its own namespace. Nothing you report is visible to any other key, and
  nothing from other keys is visible to you. The same key works for the REST Route API
  (`/v1/*`), so the REST view and the MCP view of your map are the same.

Create a private key:

```bash
curl -s -X POST https://atlas.yashdynamo.fun/v1/keys
```

```json
{"api_key": "atl_...", "namespace": "t_3f9c...", "mcp_url": "https://atlas.yashdynamo.fun/mcp", "read_only": false}
```

The key is shown once. Atlas stores only a keyed hash (HMAC-SHA256 with a server-side
secret), so a lost key cannot be recovered: create a new one.

Connection info (public):

```bash
curl -s https://atlas.yashdynamo.fun/v1/mcp-info
```

```json
{"mcp_url": "https://atlas.yashdynamo.fun/mcp", "demo_key": "...", "tools": ["atlas_find_skills", "atlas_recall", "atlas_plan", "atlas_report"]}
```

### Limits

- Key creation: a few keys per client address per day, and a total cap on keys per server.
- Requests: a per-key rate limit (per key and address for the demo key); over it, `429`.
- Storage: a maximum number of stored steps per private key; over it, `atlas_report` returns
  a `limit` error.

## Connect your agent

Atlas serves MCP over streamable HTTP only; the legacy SSE transport (`/sse`) is not offered. A
client reaches it in one of two ways: **remote (HTTP)** or **local (stdio)**. Replace `<key>`
with a private key (see above) or the public demo key.

### Remote (HTTP)

The client connects directly to the hosted URL and sends the key in a header. Use it whenever
your client supports remote MCP servers with headers. Nothing to install.

```json
{
  "mcpServers": {
    "atlas": {
      "type": "http",
      "url": "https://atlas.yashdynamo.fun/mcp",
      "headers": { "Authorization": "Bearer <key>" }
    }
  }
}
```

Some clients name the transport field differently or omit it (Cursor and Zed take just `url`
and `headers`; VS Code uses a `servers` root). The URL and the header are what matter.

**No header support?** Some clients take only a URL. Put the key in the path:

```
https://atlas.yashdynamo.fun/mcp/k/<key>
```

It is the same endpoint: same key, same namespace, same read-only rule for the demo key, same
rate limit, same session binding. **Treat that URL like a password**: anyone who has it can use
the key. Atlas masks the key in its own access log, and a wrong key gets `401` and is never
echoed. `POST /v1/keys` returns the URL ready-made as `connect_url`, and `GET /v1/mcp-info`
returns `connect_url_template` and `demo_connect_url`.

### Local (stdio)

The client launches a command and talks to it over stdin and stdout. For hosted Atlas the
command is the standard `mcp-remote` bridge, which forwards stdio to the hosted URL (it needs
Node.js):

```json
{
  "mcpServers": {
    "atlas": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://atlas.yashdynamo.fun/mcp",
               "--header", "Authorization: Bearer <key>"]
    }
  }
}
```

The same command by hand, or through the key-in-path URL when passing a header is awkward:

```bash
npx -y mcp-remote https://atlas.yashdynamo.fun/mcp --header "Authorization: Bearer <key>"
npx -y mcp-remote https://atlas.yashdynamo.fun/mcp/k/<key>
```

A self-hosted Atlas can instead run its own stdio server, `python -m atlas.mcp` (see
Self-hosting), which can also own a local browser.

### Open in your editor

Cursor and VS Code register install links, so the site's connect dialog offers one-click
buttons that add the server after a review prompt. To build the links yourself:

```js
// Cursor: name, plus the server object (not the mcpServers wrapper) as base64 JSON
const config = { url: `https://atlas.yashdynamo.fun/mcp/k/${key}` };
const cursor = "cursor://anysphere.cursor-deeplink/mcp/install?name=atlas&config="
  + encodeURIComponent(btoa(JSON.stringify(config)));

// VS Code: the server, with its name inside the JSON (vscode-insiders: for Insiders)
const server = { name: "atlas", type: "http", url: `https://atlas.yashdynamo.fun/mcp/k/${key}` };
const vscode = "vscode:mcp/install?" + encodeURIComponent(JSON.stringify(server));
```

Base64 contains `+`, `/` and `=`, so percent-encode it as shown. The VS Code JSON also works
on the command line: `code --add-mcp '{"name":"atlas","type":"http","url":"https://atlas.yashdynamo.fun/mcp/k/<key>"}'`.

### Examples

Exact lines for common clients. They are the two transports above, written for each client.

```bash
# Claude Code (header; check with `claude mcp list` or /mcp)
claude mcp add --transport http atlas https://atlas.yashdynamo.fun/mcp \
  --header "Authorization: Bearer <key>"

# Claude Code, key in the path
claude mcp add --transport http atlas https://atlas.yashdynamo.fun/mcp/k/<key>

# Codex CLI, key in the path (check with `codex mcp list`)
codex mcp add atlas --url https://atlas.yashdynamo.fun/mcp/k/<key>

# Gemini CLI
gemini mcp add --transport http --header "Authorization: Bearer <key>" atlas https://atlas.yashdynamo.fun/mcp
```

- **Claude.ai and Claude Desktop (custom connector).** Customize, Connectors, Add custom
  connector (Team and Enterprise: an owner adds it under Organization settings, Connectors).
  Enter the URL `https://atlas.yashdynamo.fun/mcp/k/<key>` and set authentication to "No sign
  in". The form also has a Request headers field, so `https://atlas.yashdynamo.fun/mcp` plus
  `Authorization: Bearer <key>` works too. The URL must use the standard HTTPS port.
- **ChatGPT (custom MCP server).** Add it from the Plugins page with the URL
  `https://atlas.yashdynamo.fun/mcp/k/<key>` and authentication "No authentication", confirm
  the risk notice, create it, then install it before using it in a conversation. Its
  authentication choices are OAuth or none, with no header field, which is why the URL route
  exists. Availability depends on your workspace permissions.
- **Codex CLI with a header.** `export ATLAS_KEY=<key>`, then in `~/.codex/config.toml`:
  `[mcp_servers.atlas]`, `url = "https://atlas.yashdynamo.fun/mcp"`,
  `bearer_token_env_var = "ATLAS_KEY"`.
- **Claude Code project file** (`.mcp.json`, `--scope project`): expand a variable instead of
  committing the key:
  `"atlas": {"type": "http", "url": "https://atlas.yashdynamo.fun/mcp", "headers": {"Authorization": "Bearer ${ATLAS_KEY}"}}`.
- **VS Code `.vscode/mcp.json`:** `"servers": {"atlas": {"type": "http", "url": "https://atlas.yashdynamo.fun/mcp", "headers": {"Authorization": "Bearer ${input:atlas-key}"}}}`
  with an `inputs` entry `{"type": "promptString", "id": "atlas-key", "description": "Atlas key", "password": true}`
  so the key is prompted, not committed.
- **Zed** (`settings.json`): `"context_servers": {"atlas": {"url": "https://atlas.yashdynamo.fun/mcp", "headers": {"Authorization": "Bearer <key>"}}}`.
- **Gemini CLI `settings.json`:** a streamable HTTP server uses `httpUrl` (`url` means SSE)
  and optional `headers`.

### Plain HTTP

```bash
curl -si https://atlas.yashdynamo.fun/mcp \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

The response carries an `mcp-session-id` header. Send it back on the following requests
(`notifications/initialized`, then `tools/list`, `tools/call`, ...). A session belongs to the
key that created it: the same session id with another key is answered `404`. To check a
deployment end to end over both routes, run
`python scripts/mcp_smoke.py --url https://atlas.yashdynamo.fun [--key <key>]`.

## Tools

The hosted server runs in planner mode: your agent drives its own browser, and Atlas plans
and learns. It never opens a browser or acts on a site itself.

| Tool | Use it to | Returns |
| --- | --- | --- |
| `atlas_find_skills` | find up to 5 things the map already knows how to do on a site that match a goal | skill ids, names, parameters, reliability |
| `atlas_plan` | get the known route from the current page (a snapshot summary) to a goal | the page's `state_id` and steps with the expected next state, or `needs_model` where the map ends |
| `atlas_recall` | find where an entity (for example `order_id` 1042) was last seen | found, the page's state id and URL template |
| `atlas_report` | report one step your agent executed and the page it led to, so the map learns it | the new page's `state_id`, whether it was known; safe to retry with the same `event_id` |

Resources: `atlas://sites` (mapped sites and their sizes) and the template
`atlas://site/{site}/changes` (recent flow changes on a site).

Every response is compact JSON within a fixed token budget; long lists are cut and flagged
`truncated`. A typical loop:

1. `atlas_find_skills` with the goal, to see what is already known.
2. `atlas_plan` with the current page summary; execute the returned steps in your browser.
3. Where the plan says `needs_model`, let your model choose the next action, execute it, and
   `atlas_report` it (with `goal_reached: true` on the last step).
4. Next time, `atlas_plan` returns the whole route.

## What is stored per key

Only what the map needs, in your key's namespace in HydraDB:

- pages as abstract states: site, URL template, title and a signature of the page's controls
  (not the page text);
- actions between pages, with `{slot}` placeholders instead of parameter values, and their
  success counts and timings;
- goals (templated text) linked to the actions that complete them, and skills compressed from
  repeated routes;
- resolved entities: type, key and the action where they were last seen;
- the ids of reported events (for idempotent retries) and detected flow changes.

The key itself is stored only as a keyed hash; the address that created a key only as a keyed
hash, to enforce the creation limit.

## Self-hosting

`uvicorn atlas.api.main:app` serves the Route API, `/mcp` and the web UI in one process.
Settings (environment):

| Variable | Meaning |
| --- | --- |
| `ATLAS_API_KEY` | admin key: read/write on `ATLAS_NAMESPACE` (default `demo`) |
| `ATLAS_DEMO_MCP_KEY` | public read-only key on `ATLAS_NAMESPACE`; unset = no demo key |
| `ATLAS_KEY_PEPPER` | secret for key hashes; unset = `POST /v1/keys` is disabled (`503`) |
| `ATLAS_KEYS_PATH` | the key store, a JSON file written atomically (default `data/mcp_keys.json`) |
| `ATLAS_PUBLIC_URL` | base URL reported as `mcp_url` (else derived from the request) |
| `TENANT_MAX` | private keys in total (default 200) |
| `TENANT_KEYS_PER_IP_DAY` | private keys per client address per day (default 3) |
| `TENANT_MAX_EVENTS` | stored steps per private key (default 5000) |
| `TENANT_REQUESTS_PER_MINUTE` | requests per key per minute (default 120) |

The key in `/mcp/k/<key>` is masked in the app's uvicorn access log. If you put a reverse proxy in
front, keep its access log off or mask that path segment in it too.

The client address is the last `X-Forwarded-For` entry, so put the app behind a
reverse proxy that overwrites that header. `python -m atlas.mcp --http` serves
the same planner-mode endpoint alone; `python -m atlas.mcp` (stdio) can also run in runner mode
with a local browser.
