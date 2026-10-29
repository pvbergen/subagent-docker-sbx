<div align="center">
  <h3>Run <a href="https://github.com/langchain-ai/deepagents">Deep Agents</a> subagents in their own <a href="https://docs.docker.com/ai/sandboxes/">Docker Sandboxes</a> (<code>sbx</code>) microVMs.</h3>
</div>


<div align="center">

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)
[![deepagents](https://img.shields.io/badge/built%20for-deepagents-orange)](https://github.com/langchain-ai/deepagents)

</div>

## subagents-docker-sbx

A Deep Agents supervisor on your host delegates to **worker agents that each run entirely inside their own sbx microVM**: the agent loop, model calls, tools and files. The host only reaches a worker over HTTP on a port published on `127.0.0.1`, through deepagents' [`AsyncSubAgent`](https://github.com/langchain-ai/deepagents). API keys never enter a VM: each one is stored as an sbx secret for that worker's sandbox alone, and the sbx proxy injects it into outgoing requests.

> **Status: experimental (0.1).** The supervisor still runs on the host and holds its own model key.

## Requirements

- [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) (`sbx`) on a machine with hardware virtualization
- Signed in: `sbx login`
- A global network policy, initialized once: `sbx policy init balanced` (or `allow-all` / `deny-all`)
- Python 3.12+ on the host

```bash
uv add subagents-docker-sbx      # or: pip install subagents-docker-sbx
```

## Quickstart

A worker is a directory with a [`langgraph.json`](https://docs.langchain.com/langsmith/application-structure) and, optionally, a `requirements.txt`:

```
workers/researcher/
├── agent.py           # graph = create_deep_agent(model="openai:gpt-5.5", ...)
├── langgraph.json     # {"dependencies": ["."], "graphs": {"researcher": "./agent.py:graph"}}
└── requirements.txt   # langchain-openai
```

```python
from deepagents import create_deep_agent
from subagents_docker_sbx import SandboxedWorker, WorkerSpec

spec = WorkerSpec(
    name="researcher",
    description="Researches a topic in an isolated sandbox.",
    worker_dir="workers/researcher",
    graph_id="researcher",
    secrets={"openai": "OPENAI_API_KEY"},  # sbx service -> host env var, scoped to this sandbox
)

with SandboxedWorker(spec) as worker:
    supervisor = create_deep_agent(model="openai:gpt-5.5", subagents=[worker.subagent()])
    supervisor.invoke({"messages": "Research the latest trends in AI and summarize them."})
```

The supervisor's model runs on the host, so the host needs that model's provider package too (here `langchain-openai`). A complete version is in [`examples/sandboxed_worker`](examples/sandboxed_worker).

## Configuration

```python
WorkerSpec(
    name="researcher",                  # subagent name the supervisor delegates to
    description="...",                  # the supervisor's model reads this to pick a worker
    graph_id="researcher",              # graph in langgraph.json
    worker_dir="workers/researcher",    # langgraph.json (+ requirements.txt); the sandbox workspace
                                        # None → mount nothing; the code comes from `template`
    secrets={"openai": "OPENAI_API_KEY"},  # stored with `sbx secret set --sandbox`; the VM sees `proxy-managed`
    env={"WORKER_MODEL": "openai:gpt-5.5"},  # extra server env; visible in the VM, never put secrets here
    allow_network=["api.tavily.com"],   # per-sandbox `sbx policy allow network`, on top of the global policy
    deny_network=["pypi.org"],          # per-sandbox denies; deny wins over any allow (`**` over every allow)
    kits=[],                            # sbx kits (`--kit`)
    agent="shell",                      # sbx agent / workload the sandbox is created from
    template=None,                      # prebuilt worker template; skips the dependency install
                                        # (required with worker_dir=None)
    memory=None, cpus=None,             # sandbox size; None → sbx defaults
    host_port=None,                     # port on host 127.0.0.1; None → a free one
)
```

`SandboxedWorker(spec)` starts the worker and blocks until it answers; `worker.subagent()` returns the `AsyncSubAgent` for the supervisor, `worker.url` its endpoint, and `worker.log_tail()` the end of the server log. `close()` (or leaving the `with` block) stops the server and removes the sandbox together with its secrets and network rules. Sandboxes are also removed at interpreter exit.

## How it works

```
host                                        sbx sandbox "subagent-researcher-<id>"
────────────────────────────                ───────────────────────────────────────
supervisor (create_deep_agent)              langgraph dev  (workers/researcher)
 └─ AsyncSubAgent(url=127.0.0.1:<port>) ───►│  ├─ model calls → sbx proxy → provider API
                                             │  │   (proxy injects the sandbox-scoped secret;
                                             │  │    the VM only sees OPENAI_API_KEY=proxy-managed)
                                             │  └─ tools / files stay inside the VM
```

Starting a worker:

1. `sbx create` with the worker directory as workspace (none with `worker_dir=None`) and the server port published on `127.0.0.1` only.
2. `sbx secret set <service> --sandbox <name>` for each secret, read from the host environment.
3. `sbx policy allow|deny network --sandbox <name>` for each network rule.
4. Unless a template is given, install the worker venv inside the VM (see below).
5. `langgraph dev` via a host-held `sbx exec` (sbx has no detached exec), from the worker directory or, with `worker_dir=None`, from `/home/agent/worker` in the template. Then wait for `/ok`.

The supervisor starts a run on the worker and polls it until it finishes. `langgraph dev` keeps threads in memory, so they end with the sandbox.

## Worker image

A worker VM needs (defined in [`requirements.py`](src/subagents_docker_sbx/requirements.py)):

- Python >= 3.11 and `uv`
- a venv at `/home/agent/venv` with `deepagents` and `langgraph-cli[inmem]`
- the worker's own `requirements.txt`, typically the model provider and tool libraries
- the sbx base-image floor: an `agent` user with UID 1000 and home `/home/agent`, `sh` and `bash`, `curl`, `git` and CA certificates

Without `template=`, the default `shell` template is used and the venv is installed on every start. To skip that, build a template once with [`docker/worker.Dockerfile`](docker/worker.Dockerfile), which turns any Debian- or Ubuntu-based image into a worker image:

```bash
docker build -f docker/worker.Dockerfile -t researcher-worker:1 workers/researcher
docker build -f docker/worker.Dockerfile --build-arg BASE_IMAGE=ubuntu:24.04 -t researcher-worker:1 workers/researcher

docker save researcher-worker:1 -o researcher-worker.tar
sbx template load researcher-worker.tar
```

The image also carries the worker's code at `/home/agent/worker`. Add a `.dockerignore` to the worker directory (for example `.env`, `.langgraph_api/`, `__pycache__/`) to keep local files out of it. Two ways to use it:

```python
# Development: dependencies from the template, code mounted from the host (no rebuild on code changes).
WorkerSpec(..., worker_dir="workers/researcher", template="researcher-worker:1")

# Isolated: nothing from the host is mounted; the worker runs the code baked into the image.
WorkerSpec(..., worker_dir=None, template="researcher-worker:1")
```

Rebuild the image when `requirements.txt` changes, and for `worker_dir=None` also when the code changes.

## Development

```bash
git clone https://github.com/pvbergen/deepagents-docker-sbx.git
cd deepagents-docker-sbx
uv sync --group integration
uv run pytest                                                   # unit tests (sbx is mocked)
SUBAGENTS_SBX_INTEGRATION=1 uv run pytest -m sbx                # real sandboxes
SUBAGENTS_SBX_INTEGRATION=1 SUBAGENTS_SBX_LLM=1 uv run pytest -m sbx   # + supervisor round trip (OPENAI_API_KEY)
SUBAGENTS_SBX_INTEGRATION=1 SUBAGENTS_SBX_TEMPLATE=researcher-worker:1 uv run pytest -m sbx  # + worker from a template
```

The integration tests need a machine where sbx can start sandboxes (hardware virtualization, `sbx login`, and an initialized `sbx policy`); they are skipped otherwise.

CI (`.github/workflows/tests.yml`) runs `ruff` and the unit tests on Python 3.12 and 3.13 for every pull request. The integration tests skip there, so run them locally before opening a PR.

## Contributing

Contributions are welcome! Please feel free to open an issue or submit a pull request.

## Security

Sandboxes are microVMs, which isolate more strongly than containers, but treat this as a development tool rather than a hard multi-tenant boundary. A mounted worker directory is shared read/write with the host; use `worker_dir=None` with a template to share nothing. Pass credentials through `secrets` so the sbx proxy injects them and they never enter the VM; anything in `env` is readable inside the sandbox. See [Deep Agents security](https://github.com/langchain-ai/deepagents?tab=security-ov-file).

## License

MIT — [LICENSE](LICENSE).