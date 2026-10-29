"""A supervisor on the host delegating to a worker agent that runs in its own sbx sandbox.

The worker (``worker/``) runs entirely inside the sandbox: its model calls, tools and files.
Its OpenAI key is stored with ``sbx secret set --sandbox`` for that sandbox only; the VM sees
``OPENAI_API_KEY=proxy-managed`` and the sbx proxy injects the real key into requests to
api.openai.com.

Prerequisites on the host:
- ``sbx login`` and an initialized ``sbx policy`` (for example ``sbx policy init balanced``).
- ``OPENAI_API_KEY`` in this shell: the host-side supervisor uses it directly, and it is
  stored as the worker sandbox's secret.

Run from the repository root:
    uv sync --group integration
    uv run python examples/sandboxed_worker/run_supervisor.py
"""

import logging
from pathlib import Path

from deepagents import create_deep_agent

from subagents_docker_sbx import SandboxedWorker, WorkerSpec

WORKER = WorkerSpec(
    name="worker",
    description="General worker agent running in an isolated sandbox.",
    worker_dir=Path(__file__).parent / "worker",
    graph_id="worker",
    secrets={"openai": "OPENAI_API_KEY"},
)
SUPERVISOR_MODEL = "openai:gpt-5.5"


def _text(content: object) -> str:
    """Plain text of a message content, which may be a string or a list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return str(content)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    with SandboxedWorker(WORKER) as worker:
        supervisor = create_deep_agent(
            model=SUPERVISOR_MODEL,
            system_prompt=(
                "You coordinate work. Delegate the user's task to the `worker` async subagent, "
                "then check on the task until it has finished and report its result."
            ),
            subagents=[worker.subagent()],
        )
        result = supervisor.invoke(
            {"messages": [{"role": "user", "content": "Name three prime numbers above 100."}]},
        )
        print(_text(result["messages"][-1].content))


if __name__ == "__main__":
    main()
