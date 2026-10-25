"""Demo: a supervisor on the host delegating to a worker agent that runs in its own sandbox.

Prerequisites on the host:
- `sbx login`, an initialized `sbx policy`, and `sbx secret set openai` (for the worker).
- `OPENAI_API_KEY` in this shell (for the host-side supervisor only).

Run from the repo root:
    uv sync --group prototype
    uv run python -m prototypes.agent_in_sbx.run_supervisor
"""

import logging
from pathlib import Path

from deepagents import AsyncSubAgent, create_deep_agent
from langgraph_sdk import get_sync_client

from .sandboxed_agent import SandboxedAgentServer

logger = logging.getLogger(__name__)

WORKER_DIR = Path(__file__).parent / "worker"
SUPERVISOR_MODEL = "openai:gpt-5.5"


def _text(content: object) -> str:
    """Plain text of a message content, which may be a string or a list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return str(content)


def check_worker_directly(url: str) -> str:
    """One round trip to the sandboxed worker through the Agent Protocol API."""
    client = get_sync_client(url=url)
    state = client.runs.wait(
        None,
        "worker",
        input={"messages": [{"role": "human", "content": "Reply with exactly: worker-ok"}]},
    )
    messages = state["messages"] if isinstance(state, dict) else []
    return _text(messages[-1]["content"]) if messages else ""


def run_supervisor(url: str) -> str:
    """Let a host-side supervisor delegate a task to the sandboxed worker."""
    supervisor = create_deep_agent(
        model=SUPERVISOR_MODEL,
        system_prompt=(
            "You coordinate work. Delegate the user's task to the `worker` async subagent, "
            "then check on the task until it has finished and report its result."
        ),
        subagents=[
            AsyncSubAgent(
                name="worker",
                description="General worker agent running in an isolated sandbox.",
                graph_id="worker",
                url=url,
            ),
        ],
    )
    result = supervisor.invoke(
        {"messages": [{"role": "user", "content": "Name three prime numbers above 100."}]},
    )
    return _text(result["messages"][-1].content)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    with SandboxedAgentServer(WORKER_DIR) as server:
        logger.info("direct call -> %s", check_worker_directly(server.url))
        logger.info("supervisor -> %s", run_supervisor(server.url))


if __name__ == "__main__":
    main()
