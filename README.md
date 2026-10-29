<div align="center">
  <h3><a href="https://docs.docker.com/ai/sandboxes/">Docker Sandboxes</a> (<code>sbx</code>) backend for <a href="https://github.com/langchain-ai/deepagents">Deep Agents</a>.</h3>
</div>


<div align="center">

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)
[![deepagents](https://img.shields.io/badge/built%20for-deepagents-orange)](https://github.com/langchain-ai/deepagents)

</div>

## deepagents-docker-sbx
Run the shell commands of [Deep Agents](https://github.com/langchain-ai/deepagents) in an isolated [Docker Sandbox](https://docs.docker.com/ai/sandboxes/) microVM instead of on your host machine.

## Quickstart

Requirements:
- [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) (`sbx`) installed on a machine with hardware virtualization
- Signed in: `sbx login`
- A global network policy, initialized once: `sbx policy init balanced` (or `allow-all` / `deny-all`)

Install with `uv`:
```bash
uv add deepagents-docker-sbx
```
or with `pip`:
```bash
pip install deepagents-docker-sbx
```

```python
from deepagents import create_deep_agent
from deepagents_docker_sbx import DockerSBX

agent = create_deep_agent(
    model="openai:gpt-5.5",
    backend=DockerSBX(),
    system_prompt="You are a research assistant.",
)

result = agent.invoke({"messages": "Research the latest trends in AI and write a summary."})
```

## Configuration

Every option is optional. Most map directly to an `sbx create` flag:

```python
DockerSBX(
    template=None,                     # sbx template image; None → the `shell` agent's template
    allow_outbound_traffic=True,       # False → per-sandbox deny-all network rule
    shared_dir="/path/to/project",     # host folder mounted into the sandbox; see note below
    timeout=120,                       # per-command timeout (seconds)
    max_output_bytes=100_000,          # per-stream stdout/stderr cap; output is streamed
                                       # and the command is killed once it is reached
    memory=None,                       # e.g. "1g"; None → sbx default (minimum 512m)
    cpus=None,                         # whole CPUs; None → sbx default
    auto_remove=True,                  # remove the sandbox on close(); False → only stop it
    extra_create_args=["--env", "FOO=bar"],  # extra `sbx create` flags
)
```

| Option | sbx behavior |
|---|---|
| `template` | Passed as `sbx create --template`. Defaults to the `shell` agent's template (`docker/sandbox-templates:shell`, Ubuntu with Python, uv, git and curl). Custom images must meet the [sbx base-image requirements](https://docs.docker.com/ai/sandboxes/customize/templates/): a non-root `agent` user (UID 1000, home `/home/agent`), `/bin/sh` and `/bin/bash`, curl, git and CA certificates. Plain images such as `python:3.12-bookworm` do not qualify. |
| `allow_outbound_traffic` | `False` adds `--deny-network "**"`, a deny-all rule scoped to this sandbox. `True` leaves egress to your global `sbx policy`; it cannot widen what that policy allows. |
| `memory` | `--memory`. sbx enforces a 512 MiB minimum. |
| `cpus` | `--cpus`. Must be a positive whole number. |
| `extra_create_args` | Appended to `sbx create` before the agent and workspace arguments. |

> [!NOTE]
> Pass an explicit `shared_dir` path to keep files after the sandbox is removed. When omitted, a temporary directory is created within the host filesystem and **removed when the DockerSBX is closed**.


## How it works

Creating a `DockerSBX` runs `sbx create --name deepagents-<id> shell <shared_dir>`, which boots a sandbox microVM with the `shell` agent (no agent binary, just a shell environment). Every shell command executed by the agent runs **inside the sandbox** via `sbx exec`, not on your host OS. Library installations, cURL downloads, and any other filesystem changes stay inside the sandbox.

The only link between the sandbox and your machine is **shared_dir**: sbx mounts it at the **same absolute path** inside the sandbox, and every command runs with that directory as its working directory. Commands run as the sandbox's non-root `agent` user (with `sudo` available).

Network access is governed by sbx policies (`sbx policy ls`). Blocked HTTP(S) requests are answered by the sbx proxy with **HTTP 403** rather than failing to connect, so check status codes (for example `curl -f`) when testing access.

What stays on the host: the agent process itself (the LangGraph loop, model calls and their API keys, and any custom Python tools) runs where you start it. The built-in file tools (`read_file`, `write_file`, `edit_file`, `ls`, `glob`, `grep`) also run on the host, but are confined to `shared_dir` (paths are resolved inside it and symlinks are not followed), so they see the same files as the sandbox.

> [!NOTE]
> The sandbox is removed automatically when the Python process exits (`atexit`). Use a context manager (below) to tear down earlier. `sbx ls` shows any sandboxes still around.


### Using a context manager

Use a context manager when you want the sandbox removed as soon as you leave the block:

```python
from deepagents import create_deep_agent
from deepagents_docker_sbx import DockerSBX

with DockerSBX() as backend:
    agent = create_deep_agent(model="openai:gpt-5.5", backend=backend)
    agent.invoke({"messages": "..."})

# Sandbox removed here.
```

## Sandboxed subagents (experimental)

`subagents_docker_sbx` goes one step further: a **whole worker agent** runs inside its own sbx
sandbox (model calls, tools, files), served by `langgraph dev`. A supervisor on the host
delegates to it through deepagents' `AsyncSubAgent` over a port published on `127.0.0.1` only.
API keys never enter the VM: each one is stored as a secret for that sandbox alone, and the sbx
proxy injects it into outgoing requests.

```python
from deepagents import create_deep_agent
from subagents_docker_sbx import SandboxedWorker, WorkerSpec

spec = WorkerSpec(
    name="researcher",
    description="Researches a topic in an isolated sandbox.",
    worker_dir="workers/researcher",        # langgraph.json (+ optional requirements.txt)
    graph_id="researcher",
    secrets={"openai": "OPENAI_API_KEY"},  # sbx service -> host env var, scoped to this sandbox
    allow_network=["api.tavily.com"],       # per-sandbox egress rules on top of `sbx policy`
    deny_network=[],                        # deny wins over any allow
)

with SandboxedWorker(spec) as worker:
    supervisor = create_deep_agent(model="openai:gpt-5.5", subagents=[worker.subagent()])
    supervisor.invoke({"messages": "Research the latest trends in AI."})
```

A worker needs inside its VM (listed in `subagents_docker_sbx/requirements.py`):

- Python >= 3.11 and `uv`
- a venv at `/home/agent/venv` with `deepagents` and `langgraph-cli[inmem]` (`WORKER_REQUIREMENTS`)
- the worker's own `requirements.txt`, typically the model provider (for example `langchain-openai`)

Without `template=`, these are installed on every start. A template that already contains the
venv (`WorkerSpec(template=...)`) skips the install.

## Examples

Prerequisites:
- An OpenAI API key
- `sbx` installed, signed in, and with an initialized network policy (see [Quickstart](#quickstart))
- Python 3.12 or higher

Set the OpenAI API key:

```bash
export OPENAI_API_KEY=your_api_key
```

### 1. Pizza agent

The [pizza agent](examples/pizza_agent.py) searches the web for a Neapolitan pizza recipe and writes it to a file in the shared folder:

```python
from deepagents import create_deep_agent
from deepagents_docker_sbx import DockerSBX

backend = DockerSBX(
    shared_dir="examples/data",
    allow_outbound_traffic=True,
)

agent = create_deep_agent(
    model="openai:gpt-5.5",
    backend=backend,
    system_prompt="You are a pizza chef.",
)

for step in agent.stream(
    {"messages": "Find the best neapolitan pizza recipe and write it to the recipe.md file."},
    stream_mode="updates",
):
    for update in step.values():
        if update and (messages := update.get("messages")):
            for message in messages:
                message.pretty_print()
```

The agent writes `recipe.md` under `examples/data/`.

### 2. Sales analyst

The [sales analyst](examples/sales_analyst.py) reads `sales.csv` from the shared folder, installs Python packages inside the sandbox as needed (for example `pandas`, `matplotlib`), runs an analysis script, and writes a markdown report with charts:

```python
from deepagents import create_deep_agent
from deepagents_docker_sbx import DockerSBX

backend = DockerSBX(
    shared_dir="examples/data",
    allow_outbound_traffic=True,
)

agent = create_deep_agent(
    model="openai:gpt-5.5",
    backend=backend,
    system_prompt="You are a sales analyst assistant.",
)

for step in agent.stream(
    {
        "messages": (
            'Analyze the "sales.csv" data and write a report (with charts) into '
            'a file called "sales_report.md". Put images in an "img" directory.'
        )
    },
    stream_mode="updates",
):
    for update in step.values():
        if update and (messages := update.get("messages")):
            for message in messages:
                message.pretty_print()
```

The agent writes `sales_report.md` and chart images under `examples/data/img/`. Installing packages needs your sbx network policy to allow `pypi.org` and `files.pythonhosted.org` (check with `sbx policy check`).

## Development

```bash
git clone https://github.com/pvbergen/deepagents-docker-sbx.git
cd deepagents-docker-sbx
uv sync
uv run pytest                                          # unit tests (sbx is mocked)
DEEPAGENTS_SBX_INTEGRATION=1 uv run pytest -m sbx      # integration tests against real sandboxes
```

The integration tests need a machine where sbx can start sandboxes (hardware virtualization, `sbx login`, and an initialized `sbx policy`); they are skipped otherwise.

CI (`.github/workflows/tests.yml`) runs `ruff` and the unit tests on Python 3.12 and 3.13 for every pull request. The integration tests skip there, so run them locally before opening a PR.

## Contributing

Contributions are welcome! Please feel free to open an issue or submit a pull request.

## Security

Sandboxes are microVMs, which isolate more strongly than containers, but treat this as a development tool rather than a hard multi-tenant boundary. Do not put secrets in the shared folder; use `sbx secret` so credentials are injected by the sbx proxy and never stored inside the sandbox. See [Deep Agents security](https://github.com/langchain-ai/deepagents?tab=security-ov-file).

## License

MIT — [LICENSE](LICENSE).

## Acknowledgements

This project started from [deepagents-docker](https://github.com/andybbruno/deepagents-docker) by [andybbruno](https://github.com/andybbruno), which runs Deep Agents commands in Docker containers. Thank you for the groundwork!
