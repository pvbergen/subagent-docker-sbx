# Plan: every agent of a workflow in its own sbx sandbox

Status: **prototype done (U1–U5 confirmed), full design not started.** Branch: `sbx-prototype`,
rebased on `main` after PR #1 (the `DockerSBX` backend) was merged.

This file is the hand-off for the next working session. Read it together with
[`README.md`](README.md) in this folder (architecture diagram, how to run, prototype results).

## Goal

`DockerSBX` (in `src/`) only sandboxes an agent's **shell commands**. The agent process itself
(LangGraph loop, model calls, API keys, custom Python tools, file tools) still runs on the host.
That is too much host access. The goal is that **each agent of a workflow runs entirely inside
its own sbx microVM**, the host only talks to it over HTTP, and API keys never enter any VM.

## What the prototype proved

| # | Question | Result |
|---|---|---|
| U1 | Can `langgraph dev` serve a Deep Agents graph inside sbx (`shell` agent, deps from PyPI)? | ✅ |
| U2 | Can the host reach it through a port published on `127.0.0.1` only? | ✅ |
| U3 | Does the real key stay out of the VM while model calls still work? | ✅ (`sbx secret set openai`; the VM sees `OPENAI_API_KEY=proxy-managed`) |
| U4 | Can a host supervisor delegate through deepagents' `AsyncSubAgent(url=...)`? | ✅ |
| U5 | Does a host-held `sbx exec` keep the server alive (sbx has no detached exec)? | ✅ |

Observations that shape the design:

- **Startup is slow:** about 50 s per sandbox, almost all of it `bootstrap.sh` installing
  dependencies inside the VM. One sandbox per agent makes this the main cost.
- **The supervisor still runs on the host** and holds a real `OPENAI_API_KEY`.
- **Delegation is polling-based:** `AsyncSubAgent` starts a run, then checks on it. Fine for
  workflows, adds latency for chatty exchanges.
- **The dev server is in-memory:** `langgraph dev` keeps threads in memory and writes
  `.langgraph_api/` into the worker dir (git-ignored). Threads are lost when the sandbox is removed.

## Unknowns still to check

| # | Question | How to check |
|---|---|---|
| U6 | Can one sandbox reach another sandbox's published port? | From sandbox A: `curl -f http://host.docker.internal:<port>/ok` with B published on `127.0.0.1:<port>`. Probably needs `sbx policy allow network --sandbox A localhost:<port>`. The 403 body explains any block. |
| U7 | Does `sbx template save` turn a bootstrapped sandbox into a reusable template, so later sandboxes skip the install? | Bootstrap once, `sbx template save`, then `sbx create -t <saved> shell <dir>` and time `/ok`. |
| U8 | Do per-sandbox secrets work? Set after create: `sbx secret set openai --sandbox <name>`, with no global secret. | Model round trip test (`DEEPAGENTS_SBX_LLM=1`) with only a sandbox-scoped secret. |
| U9 | Do per-sandbox network rules narrow egress as expected? | `sbx policy allow network --sandbox <name> api.openai.com` plus `--deny-network "**"`. Deny wins over allow, so check whether a deny-all plus a specific allow is possible at all, or whether we rely on the global `balanced` policy plus specific denies. |

## Design to build (proposal, needs the user's OK before implementing)

### Phase 1: answer U6–U9 in the prototype

Small additions to `sandboxed_agent.py` and `tests/test_prototype_agent_in_sbx.py`, each behind
the existing integration opt-in. Record the results in `README.md` like U1–U5.

### Phase 2: fast startup with a saved template

- `SandboxedAgentServer` gets a `template` option. The default flow becomes: if no saved
  template for this worker's `requirements.txt` exists, bootstrap once and `sbx template save`;
  otherwise create from the saved template and skip `bootstrap.sh`.
- Key the template name on a hash of `requirements.txt` so a dependency change rebuilds it.
- Templates stay local. **Never push anything to Docker Hub** (project hard rule).

### Phase 3: per-agent isolation settings

Configuration per agent (one dataclass, for example `AgentSandboxSpec`):

- `worker_dir` (with `langgraph.json`) and `graph_id`
- `secrets`: which services to scope to this sandbox (`sbx secret set <service> --sandbox`)
- `allow_network` / `deny_network`: per-sandbox policy rules
- `memory`, `cpus`, `host_port` (default: pick a free port automatically)

### Phase 4: sandboxed supervisor

Run the supervisor as one more sandboxed agent whose `AsyncSubAgent` URLs point at the workers
through `host.docker.internal:<port>` (depends on U6). The host process then only starts the
sandboxes, sends the initial request and reads the result. No API key on the host at all.

### Phase 5: move from prototype to package

- Move the stable parts into `src/deepagents_docker_sbx/` (for example `SandboxedAgent` plus a
  small `Workflow` helper that starts several agents and wires their URLs), with unit tests
  that mock `run_sbx`, like the existing `DockerSBX` tests.
- Keep the user-facing API small and document it in the main `README.md`.
- Decide whether `langgraph dev` is enough or a production server is needed (thread
  persistence across restarts). Check what the LangGraph production server requires before
  choosing; that was not looked into yet.

### Out of scope for now

- Streaming instead of polling between agents.
- Cloud sandboxes (`sbx --cloud`).

## Working notes for the next session

- **Project rules:** see `CLAUDE.md`. Never modify `examples/` or `.env` files; always write
  tests and use type hints; never push or alter anything on Docker Hub; tests pass before a PR.
- **Real sandboxes only run on the Mac.** The Claude Code sandbox has no `/dev/kvm`, so it can
  run unit tests only. The user runs integration tests on the Mac and pastes the results:
  ```bash
  uv sync --group prototype
  DEEPAGENTS_SBX_INTEGRATION=1 uv run pytest -m sbx tests/test_prototype_agent_in_sbx.py
  DEEPAGENTS_SBX_INTEGRATION=1 DEEPAGENTS_SBX_LLM=1 uv run pytest -m sbx tests/test_prototype_agent_in_sbx.py
  uv run python -m prototypes.agent_in_sbx.run_supervisor
  ```
- **Shared `.venv`:** the repo folder is mounted from the Mac. Inside the Claude Code sandbox,
  run uv with `UV_PROJECT_ENVIRONMENT=/tmp/deepagents-sbx-venv` so it doesn't overwrite the
  Mac's `.venv`.
- **Blocked requests return 403** from the sbx proxy instead of a connection error. Use
  `curl -f` in checks and read the response body for the reason.
- **Pushing from the Claude Code sandbox:** `origin` uses SSH, which the sandbox can't use.
  Push to `https://github.com/pvbergen/deepagents-docker-sbx.git` instead (the token needs the
  `workflow` scope to touch `.github/workflows/`).
- **CI** (`.github/workflows/tests.yml`) runs `ruff` and the unit tests on Python 3.12 and 3.13;
  integration tests skip there.
