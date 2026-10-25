# Prototype: each agent in its own sbx sandbox

`DockerSBX` sandboxes an agent's **shell commands**, but the agent process itself (LangGraph loop, model calls, API keys, custom tools) runs on the host. This prototype checks whether a whole Deep Agents agent can run **inside** its own sbx microVM, with the host only reaching it over HTTP.

```
host                                        sbx sandbox "deepagents-agent-<id>"
────────────────────────────                ──────────────────────────────────────
run_supervisor.py                           langgraph dev  (worker/agent.py:graph)
 └─ supervisor (create_deep_agent)           ├─ model calls → sbx proxy → api.openai.com
     └─ AsyncSubAgent(url=127.0.0.1:2024) ──►│   (proxy injects the key from `sbx secret`;
                                             │    the VM only sees OPENAI_API_KEY=proxy-managed)
                                             └─ tools / files stay inside the VM
```

## What it needs to show

| # | Question | How it's checked |
|---|---|---|
| U1 | Can `langgraph dev` serve a Deep Agents graph inside sbx (deps installed from PyPI)? | `test_server_answers_health_check`, `test_worker_graph_is_registered` |
| U2 | Can the host reach it through a port published on 127.0.0.1 only? | same tests, plus `test_port_is_published_on_host_loopback_only` |
| U3 | Does the real OpenAI key stay out of the VM while model calls still work? | `test_real_openai_key_never_enters_the_sandbox`, `test_worker_round_trip_through_proxy_injected_key` (`DEEPAGENTS_SBX_LLM=1`) |
| U4 | Can a host supervisor delegate through deepagents' `AsyncSubAgent`? | `run_supervisor.py` |
| U5 | Does a host-held `sbx exec` keep the server alive (sbx has no detached exec)? | the whole module-scoped test run, plus `test_close_removes_sandbox_and_port` |

## Files

- `worker/`: the agent that runs in the sandbox, mounted as the sandbox workspace.
  - `agent.py`: the graph.
  - `langgraph.json`: registers the graph as `worker`.
  - `requirements.txt`: pinned dependencies.
  - `bootstrap.sh`: installs them into `/home/agent/venv` inside the VM.
- `sandboxed_agent.py`: `SandboxedAgentServer`, which runs create → install → `langgraph dev` → readiness check → cleanup. It reuses `run_sbx` from `deepagents_docker_sbx`.
- `run_supervisor.py`: the demo. It makes one direct call to the worker, then a host-side supervisor delegates a task to it.

## Run it (on a machine where sbx can start sandboxes)

```bash
sbx secret set openai          # once: key goes to the keychain, used by the sbx proxy
export OPENAI_API_KEY=...      # host-side supervisor only
uv sync --group prototype

DEEPAGENTS_SBX_INTEGRATION=1 uv run pytest -m sbx tests/test_prototype_agent_in_sbx.py                      # U1, U2, U3 (no model call), U5
DEEPAGENTS_SBX_INTEGRATION=1 DEEPAGENTS_SBX_LLM=1 uv run pytest -m sbx tests/test_prototype_agent_in_sbx.py # + model round trip
uv run python -m prototypes.agent_in_sbx.run_supervisor                                                      # U4
```

The first start installs dependencies inside the VM, which takes a minute or two. The server log is written to a temp file on the host (`SandboxedAgentServer.log_path`), and its tail is included in startup errors.

## Results

| # | Result |
|---|---|
| U1 | ✅ `langgraph dev` serves the worker graph inside sbx; deps install from PyPI under the `balanced` policy (macOS, sbx, 2026-10-25) |
| U2 | ✅ host reaches it on `127.0.0.1:2024`; `sbx ports` shows a loopback-only binding |
| U3 | ✅ key isolation: every `OPENAI_API_KEY` in the VM is `proxy-managed`, never the host key. ⏳ model round trip through the proxy-injected key not yet run (`DEEPAGENTS_SBX_LLM=1`) |
| U4 | ⏳ pending (`run_supervisor.py`) |
| U5 | ✅ host-held `sbx exec` kept the server up for the whole test module; `close()` removes the sandbox and frees the port |

Full module (no model calls): 5 passed, 1 skipped (LLM-gated) in 52.5s, including the dependency install inside the VM.
