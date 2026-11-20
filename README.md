# Skellywix Harness Engine

A local guide router for agent harnesses. Connect your agent once, give the engine labeled task outcomes, and leave the worker running. It learns new task tags, checks the proposed routing table against separate examples, and changes the recommendations your agent actually receives. The operator desk shows the active guide index, routing misses and decision history.

The maintenance agent is deterministic, not an LLM. It requires no API key, telemetry, prompts, cloud account, or model subscription. Its worker changes only its own routing table. Native configuration edits and Hindsight connections are separate, interactive actions in the private operator desk.

## Start the desk and 24/7 worker

From a fresh clone, with Docker Compose installed:

```sh
docker compose up --build -d
```

Open **[the local operator desk](http://localhost:8760)**. Look up `test` to see a real starter guide. The worker creates its own database, checks for new evidence once a minute and resumes from the same volume after a restart. A fresh installation has four useful guides and zero observed outcomes. It doesn't treat elapsed time as evidence. The first image build needs network access; the worker itself has no network interface beyond loopback. Only the read-only desk publishes a host port, on loopback.

```sh
docker compose exec -T worker python -m skellywix status
docker compose stop       # Stop both services; keep all state.
docker compose start      # Resume the same workspace.
```

Set `SKELLYWIX_PORT` in your environment if the default port is taken. Set `SKELLYWIX_INTERVAL` to a number of seconds from 5 to 86400 to change the cadence; run `docker compose up -d` to apply. Keep the Docker service running for unattended operation. `docker compose down` preserves the named volume; adding `-v` deliberately removes it.

## Try an improvement without Docker

Requires Python 3.10+ with SQLite. No package installation or network access is needed from the source directory. On systems that name the interpreter `python3`, use that name instead. This example uses a **new, disposable** workspace with explicitly labeled demonstration data:

```sh
python -m skellywix --home demo-workspace init --demo
python -m skellywix --home demo-workspace resolve flaky
python -m skellywix --home demo-workspace run
python -m skellywix --home demo-workspace resolve flaky
python -m skellywix --home demo-workspace rollback
```

The first lookup has no guide. The pass improves from **2/3 to 3/3** checked cases and the second lookup returns `test-change`. Rollback restores the old answer; the worker won't put the rejected revision back on its next tick without new evidence. Demo cases aren't usage statistics. For a real Python workspace, run `python -m skellywix watch` (no demo flag) and, in another terminal, `python -m skellywix serve`. Both default to the same `.skellywix` directory in your home. Use `SKELLYWIX_HOME` or `--home` for another dedicated directory. Stop the foreground worker with Ctrl-C.

## Connect your existing agent

By default, the stdio MCP adapter exposes two read-only tools:

- `resolve_guide` returns the active revision and matching guide, or `guide: null` on a tie or miss. Your MCP client may send returned advice to its model. Register only guides you are willing to share.
- `harness_discovery` takes no arguments. It refreshes a local catalog of MCP declarations and returns recognized module names, evidence labels, counts, and coverage. It needs no initialized guide workspace or tool cache. It doesn't return server aliases, paths, endpoints, credentials, journal records, or private inserts. It doesn't start registered servers, send network probes, install integrations, or check their health.

For a native installation, run `python -m pip install .` in the environment your agent will use, then `skellywix init` and `skellywix watch`. Supply this server entry to an MCP client that uses `mcpServers`. If the client doesn't inherit your environment's PATH, select the installed `skellywix` executable by its full path.

```json
{"mcpServers":{"skellywix":{"command":"skellywix","args":["mcp"]}}}
```

Run the adapter on the host whose configuration you want to discover. It reads the active OMP profile, supported user registrations, and the client's launch workspace. Use `skellywix mcp --project PATH` if the client's working directory isn't the intended workspace.

For the Compose installation, the equivalent client command, run with the clone as its working directory, is:

```sh
docker compose exec -T worker python -m skellywix mcp
```

If your client can't set a working directory, pass `--project-directory` and the absolute clone path between `compose` and `exec`. Configure the adapter against **the same workspace** as the worker. The native default and the Compose volume are separate. Do not grant the model a Docker socket, a shell or broader Docker permissions to use this adapter: the operator's client launches the fixed command.

Compose discovery sees only what is available to the worker container. Use the native adapter to discover host registrations; don't mount your home directory into the worker.

Install the included [router skill](skills/skellywix-router/SKILL.md) in your harness's supported skills directory, or instruct the agent to call `resolve_guide` with non-sensitive task tags before choosing a workflow. Agents without MCP can read the identical result with `skellywix resolve test`. A null result means use the harness's normal route, not a guessed match.

## Supervise a local harness

For a trusted local client, start `skellywix mcp --supervisor-policy /absolute/path/supervisor.json`. Keep this policy in a directory only the launch operator can modify, outside the managed profiles and workspace. Select a workspace with `--project` or a nondefault Pi directory with `--pi-agent-dir`. Inventory and connection checks work without extra dependencies. To enable approved repairs, install `".[operator]"`.

Start with this policy for inventory only. It makes no API requests and authorizes no writes:

```json
{"schema":"skellywix-supervisor/1"}
```

`harness_inventory` lists OMP and Pi skill declarations, MCP registrations, cached tools, module files, OMP TTSR rules, LSP JSON overrides, and other local capability metadata. It returns up to 48 entries; pass `nextCursor` as `cursor`, with the same `kind` filter if used. Starting at `cursor: 0` refreshes the snapshot. This tool exposes component names, but not documents, journal contents, endpoints, or credentials. It doesn't activate plug-ins or check whether a declared tool is running. Use the neural map for recorded activity.

### Approve an API check

Add a `connections` array with up to one entry per module. This example selects one Hindsight bank and one Qdrant collection:

```json
{
  "schema": "skellywix-supervisor/1",
  "connections": [
    {"module":"hindsight","baseUrl":"https://memory.example.invalid","scope":"review-notes","credentialEnv":"HARNESS_BANK_KEY"},
    {"module":"qdrant","baseUrl":"http://127.0.0.1:6333","scope":"review-index"}
  ]
}
```

The approved base must match a recognized declaration, such as the enabled OMP Hindsight backend, a recognized Hindsight MCP registration, a Qdrant MCP `QDRANT_URL`, or the launch environment's `HINDSIGHT_API_URL` or `QDRANT_URL`. A cached tool or a local Qdrant disk path isn't a REST declaration. Use HTTPS with certificate verification, or HTTP on loopback. `http://localhost` uses IPv4 loopback. A credential is optional and comes only from the named launch environment variable, never from a native registration.

Each launch sends at most one [bank stats request](https://docs.hindsight.vectorize.io/api-reference/get-agent-stats/) per approved canonical Hindsight bank and one [collection details request](https://api.qdrant.tech/api-reference/collections/get-collection) per approved Qdrant collection. The check doesn't follow redirects, use proxies, retrieve records, or mutate a service. `connections` in the inventory distinguishes discovery from a successful startup read. It doesn't report current uptime or overall service health. A fresh process checks again with the same policy.

### Approve a repair

Add a `repairs` array with at most 16 entries. Each entry has a unique lowercase `id`, the exact existing local `path`, and the SHA-256 of the file's current bytes as lowercase `sha256`. Choose one operation:

| Operation | Supported updates |
| --- | --- |
| `"set":{"field":["quietStartup"],"value":true}` | One allowlisted native field. Presentation preferences in Pi and OMP, keybindings, model metadata in existing providers, MCP disable and bounded OMP request settings, and OMP LSP disable and timeout settings. Credential, transport, registration, and trust changes use the native harness. |
| `"replacement":"..."` | Exact UTF-8 text for an existing `SKILL.md`, OMP TTSR rule, or directly managed tool, hook, or extension module. Review the text before approving it. Skellywix doesn't run it or reload the harness. |

With `"automatic":true`, launch can apply only an exact, preapproved boolean presentation preference (`quietStartup`, `hideThinkingBlock`, `collapseChangelog`, or `showHardwareCursor` in Pi; `startup.showSplash` or `startup.quiet` in OMP). Everything else needs an explicit `harness_apply_repair` call.

1. Call `harness_repairs` to list approvals and read the current `revision` for a ready repair.
2. Call `harness_apply_repair` with only its `id` and `revision`. Read the receipt; `blocked` isn't a successful write. The policy, target and preimage must still match.
3. To undo, call `harness_undo_repair` with the applied `revision`. Undo restores the original bytes only in the same process and only before another edit. Receipts and undo data aren't persisted. To keep an automatic change undone across launches, remove its approval before restarting.

The supervisor refuses links, unsupported document syntax and unapproved targets. It doesn't install packages, edit auth stores, create files, remove files, reload a harness, or change its own policy. Changes to the policy block further repairs until restart. Use one writer for a managed file; plain-file revision checks and atomic replacement aren't a lock shared with other editors. Run the adapter as the profile owner, not as a service with broader permissions. It doesn't sandbox other programs running as that user.

## Teach it your workflow

1. **Register your guide.** The four starter guides already work. To add one, save a JSON object like the one below as `guide.json` and run `skellywix add-guide < guide.json` in a shell that supports input redirection. In PowerShell, use `Get-Content -Raw guide.json | skellywix add-guide`. Registration is an explicit owner action, not an automatic promotion.
2. **Record verified outcomes.** Have your test runner or review hook emit `feedback train` only after it verifies the appropriate guide. Use general task tags, never raw prompts or output. In a Compose workspace, replace `skellywix` with `docker compose exec -T worker python -m skellywix`.
3. **Supply independent checks.** An operator or separate evaluator supplies differently tagged cases with expected guide IDs. Use `none` to require abstention. Don't generate check labels by asking the router which guide it would pick. A tag set cannot be in both splits or have conflicting labels.

```json
{"id":"check-format","title":"Check document formatting","tags":["format"],"advice":"Compare the rendered document with the approved style sample before returning it."}
```

```sh
skellywix feedback train check-format layout manuscript
skellywix feedback check check-format layout proof
skellywix feedback check none unrelated
```

On the next pass, `skellywix resolve layout` returns `check-format` if the candidate passes. Idempotent feedback is safe to repeat. Until outcomes and separate checks arrive, the worker leaves the active policy alone. `run` forces one pass; `status` prints all active aliases, check misses and receipts. `export-desk` writes a standalone HTML snapshot to standard output. Snapshots and runtime state can contain **your** private guide text; keep them out of source control.

## Decision and recovery rules

- The proposer sees training cases only. It may add an unowned tag when its training examples agree on one guide. It cannot overwrite native guide tags. The router counts matching tags and abstains on ties and misses.
- The gate scores the live and proposed policy against the same check set. It requires **strictly more correct answers and zero previously correct answers lost**. Gains describe these cases, not a general model benchmark. Keep adding fresh independent checks as your harness changes.
- SQLite commits policy, revision and receipt together. Concurrent workers cannot promote the same input twice. `rollback` undoes the last promotion, not a manually registered guide. Registering a guide establishes a new owner-approved baseline.
- Bounds: 128 guides, 2,048 distinct labeled cases, eight tags per case, 128 retained decision receipts. An idle tick updates the last-pass time, not the history. At the case limit new inputs fail explicitly: preserve the workspace and start a new evaluation window.
- This is a single-owner local tool, not a hosted multiuser service. Keep the state directory private and on a local filesystem. To back up or transfer state, stop the worker and desk, copy the complete state directory or volume, then restart. Do not publish the desk through a reverse proxy or the public internet.

## Watch your harness

Run the observer on the machine that runs OMP, from the workspace you want to inspect. Install the package as above if you're running from another directory.

```sh
python -m skellywix neural-map
```

Open [the private neural map](http://127.0.0.1:8761/), then submit a prompt in your existing OMP session. **Follow latest** selects new recorded prompts. Drag to orbit the 3D view or use the view controls. **Auto-rotate** is off initially; **Pause motion** stops rotation and impulses. Reduced motion uses static highlights.

Connections light up as the journal records activity for the selected prompt. Click a node or use the component filters to inspect its source and directed connections. The view uses names such as **Hindsight - Memory** and **Qdrant - Vector Search** rather than group labels for those modules.

No setup manifest is required. The map refreshes native OMP registrations and supported declarations from Claude Code, Codex, Gemini CLI, OpenCode, Cursor, Windsurf, VS Code, Copilot, and Claude Desktop. It detects known providers from their declarations and, when available, unexpired OMP tool metadata. TOML sources require Python 3.11 or later. Files-only discovery reads supported scalar settings; it doesn't apply a running client's in-memory policy or grant access. The map also inventories hooks, rules, language and lint configuration, and local schedules. See **Discovery coverage and blind spots** for unavailable sources. `--no-native` skips installed command-line and scheduler probes. `--agent-dir`, `--project`, and `--sessions-dir` select nondefault locations.

Prompt activity comes from recent local OMP journals. The viewer attributes recorded events by parent chain. It doesn't send a model request, connect to an MCP server, run a job, or change a configuration. An MCP light means OMP recorded a tool at its address boundary, not that a separate database processed it. The viewer doesn't infer a successful lint run, inner hook, job execution, child session, or server-side span from general tool output. Older or oversized records can fall outside the live window. Prompt and response text, tool arguments and results, credentials, and raw journal IDs don't appear in the feed.

Keep any private topology in the active agent directory's `neural-map.d` folder, outside the clone. The optional `--private-dir` selects a different local folder. Each JSON file adds labeled nodes and directed edges; it isn't executable and never ships with the package. Omit secrets, endpoints, and payloads even from these files. For example:

```json
{
  "schema": "harness-neural-insert/1",
  "nodes": [
    {"kind": "automation", "name": "Index refresh"},
    {"kind": "vector", "name": "Reference collection"}
  ],
  "edges": [
    {"source": "automation:Index refresh", "target": "vector:Reference collection", "label": "Refreshes"}
  ]
}
```

Private inserts are declarations, not execution evidence. The neural map is separate from the router desk, binds only to IPv4 loopback, and has no export route. Don't proxy or publish it. Stop it with Ctrl-C.

## Edit Pi and OMP settings

From the clone, install the operator extra, then launch the desk with the project you want to inspect:

```sh
python -m pip install ".[operator]"
skellywix neural-map --operator --project .
```

Open the private link printed in your terminal and choose **Settings**. The desk discovers Pi's user and current project files, OMP's active and named profiles, native project settings, and readable OMP launch overlays. It preloads stored values, shows known defaults separately, and never saves on discovery. If both harnesses use `PI_CODING_AGENT_DIR` and the Pi directory can't be distinguished from OMP's, pass `--pi-agent-dir` for Pi.

| Harness | Editable sources | Authority |
| --- | --- | --- |
| Pi | User/project settings and MCP; user models and keybindings | [Configuration](https://docs.pi.dev/latest/configuration) and the published settings, models, and keybinding schemas bundled with the desk |
| OMP | Registered settings for the installed release; native models, MCP, keybindings, and LSP files | [Settings reference](https://github.com/can1357/oh-my-pi/blob/main/docs/settings.md) and the installed `omp config list --json` catalog from a disposable profile |

Select a source and a field. Review the exact target before saving or removing an override. **Undo last save** restores the previous file if it hasn't changed elsewhere. OMP YAML edits preserve unrelated entries and their comments. On POSIX, edited files are private to the current owner; on Windows, edits carry over the existing discretionary ACL. Do not proxy this desk, share its access link, or run it under another user's account.

The desk edits native declarations, not effective values of a running session. Project trust, environment variables, launch flags, and other providers can change what the harness uses. Auth stores, `.env` files, installed extensions, trust decisions, legacy migration sources, and plugins' own settings stay with their native tools. The editor refuses file links, unsupported YAML syntax, stale files, and edits to comment-bearing JSON. Structured model, MCP, and LSP fields are editable without launching or testing their commands. Use Pi's `/reload` or the applicable [OMP reload procedure](https://github.com/can1357/oh-my-pi/blob/main/docs/settings.md#live-file-changes) to load changes. Without an installed OMP executable or with `--no-native`, OMP registry values are read-only; operator mode never runs the map's normal native probes against live profiles.

## Visualize a Hindsight bank

In the same desk, choose **Hindsight**, review the API URL discovered from OMP settings or a [Hindsight MCP registration](https://hindsight.vectorize.io/developer/mcp-server#bank-selection), or enter an API URL yourself. The desk contacts the API only after you confirm. It never reuses stored credentials; enter a session-only API key if required. Select a bank to see its counts, memory links, entity co-occurrence, and read-only configuration snapshot. The dashboard shows up to 160 nodes and 640 links per view. The node menu provides the same inspector as the graph.

Connect to a [Hindsight Cloud or self-hosted API](https://hindsight.vectorize.io/developer/api/memory-banks), not its dashboard URL. Use HTTPS or loopback HTTP. For an explicitly trusted, keyless HTTP service on another host, pass `--hindsight-http-origin` with the exact origin. This dashboard doesn't send retain, recall, reflect, or bank mutations. Use [the official Cloud console](https://ui.hindsight.vectorize.io/) or your self-hosted console for bank administration, key management, and usage.

## Development

```sh
python -B -m unittest discover -s tests -v
```

Install `.[operator]` to include the configuration editor and Hindsight boundary tests; they are skipped in a standard-library-only checkout.
With the Compose stack running, `python tests/smoke_desk.py` checks the published loopback port from the host. Pass `--port` if you changed it.

The worker, read-only desk, and MCP adapter use the Python standard library. The operator extra adds JSON Schema validation and round-trip YAML parsing. Licensed under the [MIT License](LICENSE); bundled Pi schemas retain their upstream MIT notice.
