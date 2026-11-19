# Skellywix Harness Engine

A local guide router for agent harnesses. Connect your agent once, give the engine labeled task outcomes, and leave the worker running. It learns new task tags, checks the proposed routing table against separate examples, and changes the recommendations your agent actually receives. The operator desk shows the active guide index, routing misses and decision history.

The maintenance agent is deterministic, not an LLM. No API key, telemetry, prompts, cloud account or model subscription. It changes its own routing table, not your source code, dependencies, approval rules or model settings. A harness that doesn't call the router is unaffected.

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

The stdio MCP adapter exposes **one read-only tool**, `resolve_guide`. It returns JSON with the active revision and the matching guide's ID, title, tags and advice, or `guide: null` on a tie or miss. It never starts a model, changes routing, records feedback or asks for filesystem access. Your own MCP client may send returned guide text to its configured model. Register only guides you are willing to share with that client.

For a native installation, run `python -m pip install .` in the environment your agent will use, then `skellywix init` and `skellywix watch`. Supply this server entry to an MCP client that uses `mcpServers`. If the client doesn't inherit your environment's PATH, select the installed `skellywix` executable by its full path.

```json
{"mcpServers":{"skellywix":{"command":"skellywix","args":["mcp"]}}}
```

For the Compose installation, the equivalent client command, run with the clone as its working directory, is:

```sh
docker compose exec -T worker python -m skellywix mcp
```

If your client can't set a working directory, pass `--project-directory` and the absolute clone path between `compose` and `exec`. Configure the adapter against **the same workspace** as the worker. The native default and the Compose volume are separate. Do not grant the model a Docker socket, a shell or broader Docker permissions to use this adapter: the operator's client launches the fixed command.

Install the included [router skill](skills/skellywix-router/SKILL.md) in your harness's supported skills directory, or instruct the agent to call `resolve_guide` with non-sensitive task tags before choosing a workflow. Agents without MCP can read the identical result with `skellywix resolve test`. A null result means use the harness's normal route, not a guessed match.

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

## Development

```sh
python -B -m unittest discover -s tests -v
```

With the Compose stack running, `python tests/smoke_desk.py` checks the published loopback port from the host. Pass `--port` if you changed it.

The runtime uses the Python standard library. The optional package build uses setuptools. Licensed under the [MIT License](LICENSE).
