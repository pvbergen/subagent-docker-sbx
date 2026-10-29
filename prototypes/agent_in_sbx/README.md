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
| U6 | Can one sandbox reach another sandbox's published port? | `test_u6_sandbox_reaches_other_sandbox_published_port` (`host.docker.internal:<port>`, before and after a per-sandbox `localhost:<port>` allow) |
| U7 | Does `sbx template save` skip `bootstrap.sh` for later sandboxes? | `test_u7_template_skips_bootstrap` (times both starts) |
| U8 | Do sandbox-scoped secrets work, with no global secret? | `test_u8_secret_scoped_to_one_sandbox` (`/v1/models`: 200 with, 401 without), `test_u8_worker_round_trip_with_scoped_secret_only` (`DEEPAGENTS_SBX_LLM=1`) |
| U9 | Do per-sandbox egress rules widen and narrow as expected, set explicitly or by a v3 kit? | `test_u9_explicit_*` (on `shell` and the v3 workload), `test_u9_kit_*` (v3 mixins in `kits/`) |

## Files

- `../../PLAN.md`: status, open questions and the plan for the full design. Start here in a new session.
- `worker/`: the agent that runs in the sandbox, mounted as the sandbox workspace.
  - `agent.py`: the graph.
  - `langgraph.json`: registers the graph as `worker`.
  - `requirements.txt`: pinned dependencies.
  - `bootstrap.sh`: installs them into `/home/agent/venv` inside the VM.
- `sandboxed_agent.py`: `SandboxedAgentServer`, which runs create → install → `langgraph dev` → readiness check → cleanup. It reuses `run_sbx` from `deepagents_docker_sbx`.
- `run_supervisor.py`: the demo. It makes one direct call to the worker, then a host-side supervisor delegates a task to it.
- `probe_sandbox.py`: `ProbeSandbox`, a bare sandbox without the dependency install, for the U6–U9 checks: HTTP requests from inside the VM (a proxy refusal is a 403 whose body says why), per-sandbox policy rules and secrets.
- `kits/`: declaration-only v3 mixins for U9 (`allow-probe`, `deny-probe`, `deny-all-allow-probe`, and `get-only-probe` with `network-policy@2`). v3 mixins only compose onto a v3 workload, so they are used with `docker/sbx-kit-shell:1.0.0`, not the built-in `shell` agent (which selects a v2 kit).

## Run it (on a machine where sbx can start sandboxes)

```bash
sbx secret set openai          # once: key goes to the keychain, used by the sbx proxy
export OPENAI_API_KEY=...      # host-side supervisor only
uv sync --group prototype

DEEPAGENTS_SBX_INTEGRATION=1 uv run pytest -m sbx tests/test_prototype_agent_in_sbx.py                      # U1, U2, U3 (no model call), U5
DEEPAGENTS_SBX_INTEGRATION=1 DEEPAGENTS_SBX_LLM=1 uv run pytest -m sbx tests/test_prototype_agent_in_sbx.py # + model round trip
uv run python -m prototypes.agent_in_sbx.run_supervisor                                                      # U4

DEEPAGENTS_SBX_INTEGRATION=1 uv run pytest -m sbx tests/test_prototype_isolation.py -v -s                    # U6, U7, U9
# U8 needs only a sandbox-scoped secret: remove the global one first (`sbx secret rm openai`), keep OPENAI_API_KEY exported
DEEPAGENTS_SBX_INTEGRATION=1 DEEPAGENTS_SBX_SCOPED_SECRET=1 uv run pytest -m sbx tests/test_prototype_isolation.py -k u8 -v -s
```

The isolation tests print each observation (`[U9] kit deny pypi.org: HTTP 403: …`), which is what goes into the results table. They only add rules and secrets scoped to their own sandboxes, never global ones. U7 saves a template to the local image store and removes it afterwards. Nothing is pushed.

The first start installs dependencies inside the VM, which takes a minute or two. The server log is written to a temp file on the host (`SandboxedAgentServer.log_path`), and its tail is included in startup errors.

## Results

| # | Result |
|---|---|
| U1 | ✅ `langgraph dev` serves the worker graph inside sbx; deps install from PyPI under the `balanced` policy (macOS, sbx, 2026-10-25) |
| U2 | ✅ host reaches it on `127.0.0.1:2024`; `sbx ports` shows a loopback-only binding |
| U3 | ✅ key isolation: every `OPENAI_API_KEY` in the VM is `proxy-managed`, never the host key; ✅ model round trip works with the key injected by the sbx proxy (`sbx secret set openai`) |
| U4 | ✅ host supervisor delegated through `AsyncSubAgent`: created a thread on the sandboxed worker, started the run, polled it to completion and returned the worker's answer (`101, 103, 107`) |
| U5 | ✅ host-held `sbx exec` kept the server up for the whole test module; `close()` removes the sandbox and frees the port |
| U6 | ✅ another sandbox reaches the published port at `host.docker.internal:<port>` once it has a per-sandbox `sbx policy allow network --sandbox <caller> localhost:<port>` (`/ok` → 200). Without it the proxy answers 403 "Approval required for localhost:<port>" and files a pending approval. The rule doesn't leak: a third sandbox stays at 403 |
| U7 | ✅ a sandbox created with `--template` from a saved bootstrapped sandbox has the venv and serves `/ok` without running `bootstrap.sh`: 5.8 s vs 10.5 s with bootstrap (warm uv cache). `sbx template rm` asks for confirmation on stdin |
| U8 | ✅ with no global `openai` secret, `sbx secret set openai --sandbox <name> -t …` after create takes effect immediately: `/v1/models` → 200 in that sandbox, 401 in a sandbox without it. A full worker round trip (`DEEPAGENTS_SBX_LLM=1`) works with only the sandbox-scoped secret. The key lives only in the host's environment and the sbx secret store, never in a VM |
| U9 | ✅ per-sandbox egress rules work from both sources, explicit (`sbx policy allow|deny network --sandbox`) and v3 kit (`network-policy@1/@2`). **Allow** widens one sandbox only: httpbin.org 403 → 200 with the rule, still 403 in a sandbox without it (explicit on `shell` and `docker/sbx-kit-shell:1.0.0`, and via kit). **Bounded allow** (`network-policy@2`, GET only) passes GET and refuses POST with 403. **Deny** narrows below the global policy: pypi.org 403 "Blocked by local rule" while a sandbox without the rule gets 200. **Deny `**` wins over any allow**, explicit and kit, so "deny all + allow a few" is not possible: the design has to be the global policy plus specific denies (or bounded allows). Per-sandbox rules are gone after `sbx rm`. Hosts outside every allow list get 403 "Approval required" and leave a pending approval. example.com was unusable as a probe host (proxy 502 upstream) |

First isolation run: Linux host, 9 passed, 4 failed (the allow checks above), 2 skipped (U8), 240 s.

Full module (no model calls): 5 passed, 1 skipped (LLM-gated) in 52.5s, including the dependency install inside the VM.
With `DEEPAGENTS_SBX_LLM=1`: 6 passed in 63.1s.
